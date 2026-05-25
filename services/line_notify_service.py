import json
import re

import requests
from flask import current_app

from services.code_service import now_iso
from services.line_bind_service import (
    get_active_binding_by_role_target,
    get_active_binding_by_contact_code,
)


DANGEROUS_PATTERNS = [
    r"/admin",
    r"admin_token\s*=",
    r"token\s*=",
    r"ADMIN_TOKEN",
    r"FGO_INTERNAL_SECRET",
    r"LINE_CHANNEL_ACCESS_TOKEN",
    r"SECRET_KEY",
]


def _gateway_base_url() -> str:
    return (
        current_app.config.get("LINEHOOK_BASE_URL")
        or current_app.config.get("LINE_GATEWAY_BASE_URL")
        or ""
    ).rstrip("/")


def _internal_secret() -> str:
    return current_app.config.get("FGO_INTERNAL_SECRET", "")


def _admin_line_user_id() -> str:
    return (current_app.config.get("FGO_ADMIN_LINE_USER_ID") or "").strip()


def _safe_text(value, default=""):
    try:
        value = str(value if value is not None else default).strip()
        return value if value else default
    except Exception:
        return default


def _money(value):
    try:
        return max(0, int(value or 0))
    except Exception:
        return 0


def _format_twd(value):
    return f"{_money(value):,} TWD"


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
        pass

    try:
        value = getattr(row, key)
        return default if value is None else value
    except Exception:
        return default


def _settlement_value(settlement, key, default=""):
    return _row_get(settlement, key, default)


def _role_label(role):
    role = _safe_text(role).upper()

    if role == "STORE":
        return "店家"

    if role == "DRIVER":
        return "Shiper"

    if role == "CUSTOMER":
        return "客戶"

    if role == "ADMIN":
        return "Admin"

    return role or "用戶"


def _target_display_name(target=None, *, role="", fallback=""):
    role = _safe_text(role).upper()

    if role == "STORE":
        return (
            _safe_text(_row_get(target, "store_name", ""))
            or _safe_text(_row_get(target, "display_name", ""))
            or _safe_text(_row_get(target, "target_code", ""))
            or fallback
            or "店家"
        )

    if role == "DRIVER":
        return (
            _safe_text(_row_get(target, "driver_name", ""))
            or _safe_text(_row_get(target, "display_name", ""))
            or _safe_text(_row_get(target, "target_code", ""))
            or fallback
            or "Shiper"
        )

    return (
        _safe_text(_row_get(target, "display_name", ""))
        or _safe_text(_row_get(target, "target_code", ""))
        or fallback
        or _role_label(role)
    )


def _target_code_from(target=None, settlement=None, explicit=""):
    return (
        _safe_text(explicit)
        or _safe_text(_row_get(target, "store_code", ""))
        or _safe_text(_row_get(target, "driver_code", ""))
        or _safe_text(_row_get(target, "target_code", ""))
        or _safe_text(_settlement_value(settlement, "target_code", ""))
    )


def _settlement_code(settlement=None):
    return _safe_text(_settlement_value(settlement, "settlement_code", ""))


def _settlement_amount(settlement=None, amount_twd=None):
    if amount_twd is not None:
        return _money(amount_twd)

    return _money(_settlement_value(settlement, "amount_twd", 0))


def sanitize_line_text(text: str) -> str:
    text = str(text or "")

    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return (
                "FUMAP GO 通知：此訊息包含後台或安全連結，已被系統隱藏。"
                "請回到 webapp 或聯絡客服。"
            )

    return text[:4500]


def _log_push(
    db,
    *,
    contact_code="",
    line_user_id="",
    event_type="GENERAL",
    target_role="",
    target_code="",
    order_code="",
    message_preview="",
    push_status="SKIPPED",
    gateway_response=None,
    commit=True,
):
    try:
        now = now_iso()

        db.execute(
            """
            INSERT INTO line_push_logs (
                contact_code,
                line_user_id,
                event_type,
                target_role,
                target_code,
                order_code,
                message_preview,
                push_status,
                gateway_response,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contact_code or "",
                line_user_id or "",
                event_type or "GENERAL",
                target_role or "",
                target_code or "",
                order_code or "",
                str(message_preview or "")[:300],
                push_status or "SKIPPED",
                json.dumps(gateway_response or {}, ensure_ascii=False)[:1500],
                now,
            ),
        )

        if commit:
            db.commit()

    except Exception as exc:
        print(f"[LINE][LOG][ERROR] failed to write line_push_logs: {exc}")


def push_text_to_line_user(line_user_id: str, text: str):
    """
    Webapp calls LINE Gateway, not LINE Messaging API directly.

    Required ENV:
    - LINEHOOK_BASE_URL or LINE_GATEWAY_BASE_URL
    - FGO_INTERNAL_SECRET
    """
    line_user_id = (line_user_id or "").strip()
    text = sanitize_line_text(text)

    if not line_user_id:
        return {
            "ok": False,
            "skipped": True,
            "error": "line_user_id empty",
        }

    base = _gateway_base_url()

    if not base:
        return {
            "ok": False,
            "skipped": True,
            "error": "LINEHOOK_BASE_URL not set",
        }

    try:
        response = requests.post(
            f"{base}/internal/push",
            headers={
                "Content-Type": "application/json",
                "X-FGO-INTERNAL-SECRET": _internal_secret(),
                "X-FGO-Internal-Secret": _internal_secret(),
            },
            data=json.dumps(
                {
                    "to": line_user_id,
                    "text": text,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            timeout=10,
        )

        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:500]}

        return {
            "ok": bool(response.ok),
            "status_code": response.status_code,
            "body": body,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


def push_image_to_line_user(line_user_id: str, image_url: str, text: str = ""):
    """
    Push image through LINE Gateway.

    Rule:
    - image_url must be public HTTPS.
    - Do not push private/local image paths.
    """
    line_user_id = (line_user_id or "").strip()
    image_url = (image_url or "").strip()
    text = sanitize_line_text(text)

    if not line_user_id:
        return {
            "ok": False,
            "skipped": True,
            "error": "line_user_id empty",
        }

    if not image_url.startswith("https://"):
        return {
            "ok": False,
            "skipped": True,
            "error": "image_url must be public HTTPS",
        }

    base = _gateway_base_url()

    if not base:
        return {
            "ok": False,
            "skipped": True,
            "error": "LINEHOOK_BASE_URL not set",
        }

    try:
        response = requests.post(
            f"{base}/internal/push-image",
            headers={
                "Content-Type": "application/json",
                "X-FGO-INTERNAL-SECRET": _internal_secret(),
                "X-FGO-Internal-Secret": _internal_secret(),
            },
            data=json.dumps(
                {
                    "to": line_user_id,
                    "image_url": image_url,
                    "preview_url": image_url,
                    "text": text,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            timeout=10,
        )

        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:500]}

        return {
            "ok": bool(response.ok),
            "status_code": response.status_code,
            "body": body,
        }

    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


def push_admin_direct(
    db,
    *,
    event_type="ADMIN_DIRECT",
    message="",
    order_code="",
    commit=True,
):
    """
    Push directly to Admin LINE userId via FGO_ADMIN_LINE_USER_ID.

    This does not require an ADMIN binding row.
    """
    admin_line_user_id = _admin_line_user_id()
    safe_message = sanitize_line_text(message)

    if not admin_line_user_id:
        result = {
            "ok": False,
            "skipped": True,
            "error": "FGO_ADMIN_LINE_USER_ID not set",
        }

        _log_push(
            db,
            line_user_id="",
            event_type=event_type,
            target_role="ADMIN",
            target_code="ADMIN_DIRECT",
            order_code=order_code,
            message_preview=safe_message,
            push_status="SKIPPED_NO_ADMIN_LINE_USER_ID",
            gateway_response=result,
            commit=commit,
        )

        return result

    if not admin_line_user_id.startswith("U"):
        result = {
            "ok": False,
            "skipped": True,
            "error": "FGO_ADMIN_LINE_USER_ID must start with U",
        }

        _log_push(
            db,
            line_user_id=admin_line_user_id,
            event_type=event_type,
            target_role="ADMIN",
            target_code="ADMIN_DIRECT",
            order_code=order_code,
            message_preview=safe_message,
            push_status="SKIPPED_INVALID_ADMIN_LINE_USER_ID",
            gateway_response=result,
            commit=commit,
        )

        return result

    result = push_text_to_line_user(admin_line_user_id, safe_message)
    push_status = "SUCCESS" if result.get("ok") else "FAILED"

    _log_push(
        db,
        line_user_id=admin_line_user_id,
        event_type=event_type,
        target_role="ADMIN",
        target_code="ADMIN_DIRECT",
        order_code=order_code,
        message_preview=safe_message,
        push_status=push_status,
        gateway_response=result,
        commit=commit,
    )

    return result


def push_to_binding(
    db,
    binding,
    *,
    event_type="GENERAL",
    message="",
    order_code="",
    commit=True,
):
    if not binding:
        _log_push(
            db,
            event_type=event_type,
            order_code=order_code,
            message_preview=message,
            push_status="SKIPPED_NO_BINDING",
            gateway_response={"error": "no active binding"},
            commit=commit,
        )

        return {
            "ok": False,
            "skipped": True,
            "error": "no active binding",
        }

    if binding["status"] != "ACTIVE" or not binding["line_user_id"]:
        _log_push(
            db,
            contact_code=binding["contact_code"],
            line_user_id=binding["line_user_id"],
            event_type=event_type,
            target_role=binding["role"],
            target_code=binding["target_code"],
            order_code=order_code,
            message_preview=message,
            push_status="SKIPPED_INACTIVE_BINDING",
            gateway_response={"status": binding["status"]},
            commit=commit,
        )

        return {
            "ok": False,
            "skipped": True,
            "error": "binding inactive",
        }

    result = push_text_to_line_user(binding["line_user_id"], message)
    push_status = "SUCCESS" if result.get("ok") else "FAILED"

    _log_push(
        db,
        contact_code=binding["contact_code"],
        line_user_id=binding["line_user_id"],
        event_type=event_type,
        target_role=binding["role"],
        target_code=binding["target_code"],
        order_code=order_code,
        message_preview=message,
        push_status=push_status,
        gateway_response=result,
        commit=commit,
    )

    return result

def push_to_role_target(
    db,
    *,
    role,
    target_code,
    event_type="GENERAL",
    message="",
    order_code="",
    commit=True,
):
    binding = get_active_binding_by_role_target(db, role, target_code)

    return push_to_binding(
        db,
        binding,
        event_type=event_type,
        message=message,
        order_code=order_code,
        commit=commit,
    )


def push_customer_if_bound(
    db,
    *,
    customer_user_id,
    event_type="CUSTOMER_NOTIFICATION",
    message="",
    order_code="",
    commit=True,
):
    """
    Push to CUSTOMER if LINE is already bound.

    Safe skip if customer has no binding.
    """
    customer_user_id = str(customer_user_id or "").strip()

    if not customer_user_id:
        _log_push(
            db,
            event_type=event_type,
            target_role="CUSTOMER",
            target_code="",
            order_code=order_code,
            message_preview=message,
            push_status="SKIPPED_NO_CUSTOMER_USER_ID",
            gateway_response={"error": "customer_user_id empty"},
            commit=commit,
        )

        return {
            "ok": False,
            "skipped": True,
            "error": "customer_user_id empty",
        }

    return push_to_role_target(
        db,
        role="CUSTOMER",
        target_code=f"CUS-{customer_user_id}",
        event_type=event_type,
        message=message,
        order_code=order_code,
        commit=commit,
    )


def push_user_bind_success(
    db,
    *,
    role,
    target_code,
    line_display_name="",
    commit=True,
):
    """
    Push bind success message to the user after One-Tap bind.

    This relies on the binding already being active in line_contact_bindings.
    """
    role = (role or "").strip().upper()
    target_code = (target_code or "").strip()
    line_display_name = (line_display_name or "").strip()

    message = (
        "FUMAP GO LINE 綁定成功\n"
        f"角色：{role}\n"
        f"代碼：{target_code}\n"
        f"LINE 名稱：{line_display_name or '-'}\n"
        "之後重要通知會同時透過 Email 與 LINE 發送。"
    )

    return push_to_role_target(
        db,
        role=role,
        target_code=target_code,
        event_type="LINE_BIND_SUCCESS",
        message=message,
        order_code="",
        commit=commit,
    )


def push_to_contact_code(
    db,
    *,
    contact_code,
    event_type="GENERAL",
    message="",
    order_code="",
    commit=True,
):
    binding = get_active_binding_by_contact_code(db, contact_code)

    return push_to_binding(
        db,
        binding,
        event_type=event_type,
        message=message,
        order_code=order_code,
        commit=commit,
    )


def push_order_event(
    db,
    *,
    order,
    target_role,
    target_code,
    event_type,
    message,
    commit=True,
):
    order_code = ""

    try:
        order_code = order["order_code"]
    except Exception:
        order_code = ""

    return push_to_role_target(
        db,
        role=target_role,
        target_code=target_code,
        event_type=event_type,
        message=message,
        order_code=order_code,
        commit=commit,
    )


def build_basic_order_message(order, title="FUMAP GO 訂單通知"):
    def get(key, default=""):
        try:
            return order[key]
        except Exception:
            return default

    lines = [
        f"🔔 {title}",
        "",
        f"訂單：{get('order_code')}",
        f"狀態：{get('status')}",
        f"金額：{get('total_twd', 0)} TWD",
    ]

    if get("delivery_address"):
        lines.append(f"地址：{get('delivery_address')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------
# Phase 6 Lite settlement LINE helpers
# ---------------------------------------------------------------------


def build_admin_payout_requested_message(
    *,
    role="",
    target_code="",
    target_name="",
    settlement=None,
    amount_twd=None,
    note="",
):
    role = _safe_text(role or _settlement_value(settlement, "role", "")).upper()
    role_label = _role_label(role)
    target_code = _safe_text(target_code or _settlement_value(settlement, "target_code", ""))
    target_name = _safe_text(target_name or target_code or "-")
    settlement_code = _settlement_code(settlement) or "-"
    amount_text = _format_twd(_settlement_amount(settlement, amount_twd))
    note = _safe_text(note or _settlement_value(settlement, "note", "")) or "-"

    return "\n".join(
        [
            "FUMAP GO 結算通知",
            f"{role_label}申請 Admin 付款",
            "",
            f"對象：{target_name}",
            f"代碼：{target_code or '-'}",
            f"結算單：{settlement_code}",
            f"申請金額：{amount_text}",
            f"備註：{note}",
            "",
            "請登入 Admin 後台核對金額與銀行帳戶。",
            "確認轉帳完成後，再於系統按「確認已付款完成」。",
        ]
    )


def push_admin_payout_requested(
    db,
    *,
    target=None,
    settlement=None,
    role="",
    target_code="",
    target_name="",
    amount_twd=None,
    note="",
    commit=True,
):
    """
    Store/Driver requests Admin payout.

    LINE target:
    - Admin direct LINE userId.

    Email should be sent separately by email_service first.
    LINE is a supplementary channel.
    """
    role = _safe_text(role or _settlement_value(settlement, "role", "")).upper()
    target_code = _target_code_from(target, settlement, target_code)
    target_name = _safe_text(target_name) or _target_display_name(
        target,
        role=role,
        fallback=target_code,
    )
    settlement_code = _settlement_code(settlement)

    message = build_admin_payout_requested_message(
        role=role,
        target_code=target_code,
        target_name=target_name,
        settlement=settlement,
        amount_twd=amount_twd,
        note=note,
    )

    return push_admin_direct(
        db,
        event_type="ADMIN_PAYOUT_REQUESTED",
        message=message,
        order_code=settlement_code,
        commit=commit,
    )


def build_admin_target_marked_paid_message(
    *,
    role="",
    target_code="",
    target_name="",
    settlement=None,
    payment_method="",
    note="",
):
    role = _safe_text(role or _settlement_value(settlement, "role", "")).upper()
    role_label = _role_label(role)
    target_code = _safe_text(target_code or _settlement_value(settlement, "target_code", ""))
    target_name = _safe_text(target_name or target_code or "-")
    settlement_code = _settlement_code(settlement) or "-"
    amount_text = _format_twd(_settlement_amount(settlement))
    marked_paid_at = _safe_text(_settlement_value(settlement, "target_marked_paid_at", ""))
    payment_method = _safe_text(
        payment_method
        or _settlement_value(settlement, "target_payment_method", "")
        or "BANK_TRANSFER"
    )
    note = _safe_text(note or _settlement_value(settlement, "target_marked_paid_note", "")) or "-"

    return "\n".join(
        [
            "FUMAP GO 結算通知",
            f"{role_label}已回報付款",
            "",
            f"對象：{target_name}",
            f"代碼：{target_code or '-'}",
            f"結算單：{settlement_code}",
            f"回報金額：{amount_text}",
            f"付款方式：{payment_method}",
            f"回報時間：{marked_paid_at or '-'}",
            f"備註：{note}",
            "",
            "此狀態只代表對方已回報付款，不代表系統已結算。",
            "請確認實際入帳後，再於 Admin 後台按「確認已收款」。",
        ]
    )


def push_admin_settlement_target_marked_paid(
    db,
    *,
    target=None,
    settlement=None,
    role="",
    target_code="",
    target_name="",
    payment_method="",
    note="",
    commit=True,
):
    """
    Store/Driver clicked 我已付款.

    Critical:
    - This only notifies Admin.
    - It does not confirm the settlement.
    - Admin must verify payment and press confirm-paid.
    """
    role = _safe_text(role or _settlement_value(settlement, "role", "")).upper()
    target_code = _target_code_from(target, settlement, target_code)
    target_name = _safe_text(target_name) or _target_display_name(
        target,
        role=role,
        fallback=target_code,
    )
    settlement_code = _settlement_code(settlement)

    message = build_admin_target_marked_paid_message(
        role=role,
        target_code=target_code,
        target_name=target_name,
        settlement=settlement,
        payment_method=payment_method,
        note=note,
    )

    return push_admin_direct(
        db,
        event_type="SETTLEMENT_TARGET_MARKED_PAID",
        message=message,
        order_code=settlement_code,
        commit=commit,
    )


def build_target_payment_request_message(
    *,
    role="",
    target_name="",
    settlement=None,
    admin_payment_info=None,
):
    role = _safe_text(role or _settlement_value(settlement, "role", "")).upper()
    role_label = _role_label(role)
    target_name = _safe_text(target_name) or role_label
    settlement_code = _settlement_code(settlement) or "-"
    amount_text = _format_twd(_settlement_amount(settlement))
    admin_payment_info = admin_payment_info or {}

    bank_name = _safe_text(admin_payment_info.get("bank_name", ""))
    bank_code = _safe_text(admin_payment_info.get("bank_code", ""))
    bank_account = _safe_text(admin_payment_info.get("bank_account", ""))
    bank_note = _safe_text(admin_payment_info.get("bank_note", ""))

    return "\n".join(
        [
            "FUMAP GO 結算通知",
            f"{target_name} 您好",
            "",
            f"本次{role_label}需支付 Admin 的平台費：{amount_text}",
            f"結算單：{settlement_code}",
            "",
            "Admin 收款帳戶：",
            f"銀行：{bank_name or '-'}",
            f"代碼：{bank_code or '-'}",
            f"帳號：{bank_account or '-'}",
            f"備註：{bank_note or '-'}",
            "",
            "完成轉帳後，請到對帳頁按「我已付款」。",
            "Admin 確認入帳後，系統才會完成結算。",
        ]
    )


def push_settlement_payment_request_to_target(
    db,
    *,
    role,
    target_code,
    target=None,
    settlement=None,
    admin_payment_info=None,
    commit=True,
):
    """
    Admin asks Store/Driver to pay service/platform fee.

    LINE target:
    - Store/Driver if bound.
    """
    role = _safe_text(role or _settlement_value(settlement, "role", "")).upper()
    target_code = _target_code_from(target, settlement, target_code)
    target_name = _target_display_name(target, role=role, fallback=target_code)
    settlement_code = _settlement_code(settlement)

    if role == "STORE":
        event_type = "STORE_SETTLEMENT_PAYMENT_REQUEST"
    elif role == "DRIVER":
        event_type = "DRIVER_SETTLEMENT_PAYMENT_REQUEST"
    else:
        event_type = "SETTLEMENT_PAYMENT_REQUEST"

    message = build_target_payment_request_message(
        role=role,
        target_name=target_name,
        settlement=settlement,
        admin_payment_info=admin_payment_info,
    )

    return push_to_role_target(
        db,
        role=role,
        target_code=target_code,
        event_type=event_type,
        message=message,
        order_code=settlement_code,
        commit=commit,
    )


def build_target_settlement_confirmed_message(
    *,
    role="",
    target_name="",
    settlement=None,
):
    role = _safe_text(role or _settlement_value(settlement, "role", "")).upper()
    direction = _safe_text(_settlement_value(settlement, "direction", "")).upper()
    role_label = _role_label(role)
    target_name = _safe_text(target_name) or role_label
    settlement_code = _settlement_code(settlement) or "-"
    amount_text = _format_twd(_settlement_amount(settlement))
    confirmed_at = _safe_text(_settlement_value(settlement, "paid_confirmed_at", ""))

    if direction == "ADMIN_OWES_TARGET":
        title = "Admin 已確認付款完成"
        detail = "若此為全額結算，對帳頁的「Admin 應付款」金額會自動歸零。"
    elif direction == "TARGET_OWES_ADMIN":
        title = "Admin 已確認收款完成"
        detail = "若此為全額結算，對帳頁的「我欠 Admin」金額會自動歸零。"
    else:
        title = "結算已確認完成"
        detail = "請到對帳頁查看最新結算狀態。"

    return "\n".join(
        [
            "FUMAP GO 結算通知",
            f"{target_name} 您好",
            "",
            title,
            f"角色：{role_label}",
            f"結算單：{settlement_code}",
            f"金額：{amount_text}",
            f"確認時間：{confirmed_at or '-'}",
            "",
            detail,
        ]
    )


def push_target_settlement_confirmed(
    db,
    *,
    role,
    target_code,
    target=None,
    settlement=None,
    commit=True,
):
    """
    Admin confirmed settlement paid/received.

    LINE target:
    - Store/Driver if bound.

    Debt becomes reduced because settlement status should already be
    PAID_CONFIRMED in settlement_service.
    """
    role = _safe_text(role or _settlement_value(settlement, "role", "")).upper()
    target_code = _target_code_from(target, settlement, target_code)
    target_name = _target_display_name(target, role=role, fallback=target_code)
    settlement_code = _settlement_code(settlement)

    direction = _safe_text(_settlement_value(settlement, "direction", "")).upper()

    if direction == "ADMIN_OWES_TARGET":
        event_type = "SETTLEMENT_ADMIN_PAYOUT_CONFIRMED"
    elif direction == "TARGET_OWES_ADMIN":
        event_type = "SETTLEMENT_ADMIN_RECEIPT_CONFIRMED"
    else:
        event_type = "SETTLEMENT_PAID_CONFIRMED"

    message = build_target_settlement_confirmed_message(
        role=role,
        target_name=target_name,
        settlement=settlement,
    )

    return push_to_role_target(
        db,
        role=role,
        target_code=target_code,
        event_type=event_type,
        message=message,
        order_code=settlement_code,
        commit=commit,
    )


def push_store_payment_request(
    db,
    *,
    store_code,
    store=None,
    settlement=None,
    admin_payment_info=None,
    commit=True,
):
    return push_settlement_payment_request_to_target(
        db,
        role="STORE",
        target_code=store_code,
        target=store,
        settlement=settlement,
        admin_payment_info=admin_payment_info,
        commit=commit,
    )


def push_driver_payment_request(
    db,
    *,
    driver_code,
    driver=None,
    settlement=None,
    admin_payment_info=None,
    commit=True,
):
    return push_settlement_payment_request_to_target(
        db,
        role="DRIVER",
        target_code=driver_code,
        target=driver,
        settlement=settlement,
        admin_payment_info=admin_payment_info,
        commit=commit,
    )


def push_store_settlement_confirmed(
    db,
    *,
    store_code,
    store=None,
    settlement=None,
    commit=True,
):
    return push_target_settlement_confirmed(
        db,
        role="STORE",
        target_code=store_code,
        target=store,
        settlement=settlement,
        commit=commit,
    )


def push_driver_settlement_confirmed(
    db,
    *,
    driver_code,
    driver=None,
    settlement=None,
    commit=True,
):
    return push_target_settlement_confirmed(
        db,
        role="DRIVER",
        target_code=driver_code,
        target=driver,
        settlement=settlement,
        commit=commit,
    )

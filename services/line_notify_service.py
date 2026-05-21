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
    - LINEHOOK_BASE_URL
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

    V1/V2 rule:
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
    It is used for One-Tap LINE bind, payment proof alerts, and urgent admin alerts.

    Required ENV:
    - FGO_ADMIN_LINE_USER_ID must be a real LINE userId starting with U...
    - LINEHOOK_BASE_URL
    - FGO_INTERNAL_SECRET
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

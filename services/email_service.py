import html
import smtplib
from email.message import EmailMessage

from flask import current_app

from db import get_db


EMAIL_STATUS_SENT = "SENT"
EMAIL_STATUS_FAILED = "FAILED"
EMAIL_STATUS_SKIPPED = "SKIPPED"


def _safe_str(value):
    return "" if value is None else str(value)


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
        return row.get(key, default)
    except Exception:
        return default


def _order_value(order, key, default=""):
    if order is None:
        return default

    try:
        if key in order.keys():
            value = order[key]
            return default if value is None else value
    except Exception:
        pass

    try:
        value = order.get(key, default)
        return default if value is None else value
    except Exception:
        pass

    try:
        value = getattr(order, key)
        return default if value is None else value
    except Exception:
        return default


def _format_twd(value):
    try:
        amount = int(value or 0)
        return f"{amount:,} TWD"
    except Exception:
        return "0 TWD"


def _smtp_config():
    return {
        "host": (current_app.config.get("SMTP_HOST") or "").strip(),
        "port": int(current_app.config.get("SMTP_PORT") or 587),
        "username": (current_app.config.get("SMTP_USERNAME") or "").strip(),
        "password": (current_app.config.get("SMTP_PASSWORD") or "").strip(),
        "use_tls": bool(current_app.config.get("SMTP_USE_TLS", True)),
        "from_email": (current_app.config.get("SMTP_FROM_EMAIL") or "").strip(),
    }


def smtp_is_configured():
    cfg = _smtp_config()

    return bool(
        cfg["host"]
        and cfg["port"]
        and cfg["username"]
        and cfg["password"]
        and cfg["from_email"]
    )


def normalize_email(email):
    return (email or "").strip().lower()


def is_valid_email(email):
    email = normalize_email(email)

    if not email:
        return False

    if len(email) > 255:
        return False

    if "@" not in email:
        return False

    local, _, domain = email.partition("@")

    if not local or not domain:
        return False

    if "." not in domain:
        return False

    return True


def get_admin_notify_emails():
    emails_raw = (current_app.config.get("ADMIN_NOTIFY_EMAILS") or "").strip()

    if not emails_raw:
        emails_raw = (current_app.config.get("ADMIN_NOTIFY_EMAIL") or "").strip()

    emails = []
    seen = set()

    for item in emails_raw.split(","):
        email = normalize_email(item)

        if not email:
            continue

        if email in seen:
            continue

        seen.add(email)
        emails.append(email)

    return emails


def absolute_url(path_or_url):
    value = (path_or_url or "").strip()

    if not value:
        return ""

    if value.startswith("http://") or value.startswith("https://"):
        return value

    base_url = (
        current_app.config.get("APP_BASE_URL")
        or current_app.config.get("PUBLIC_BASE_URL")
        or ""
    ).strip().rstrip("/")

    if not base_url:
        return value

    if not value.startswith("/"):
        value = "/" + value

    return f"{base_url}{value}"


def _info_rows_html(info_rows):
    if not info_rows:
        return ""

    rows = []

    if isinstance(info_rows, dict):
        iterable = info_rows.items()
    else:
        iterable = info_rows

    for item in iterable:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            label, value = item[0], item[1]
        else:
            continue

        rows.append(
            f"""
            <tr>
              <td style="padding:8px 0;color:#6B7280;font-size:14px;width:38%;">
                {html.escape(_safe_str(label))}
              </td>
              <td style="padding:8px 0;color:#1F2937;font-size:14px;font-weight:600;text-align:right;">
                {html.escape(_safe_str(value))}
              </td>
            </tr>
            """
        )

    if not rows:
        return ""

    return f"""
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border-collapse:collapse;margin:18px 0;padding:0;border-top:1px solid #E5E7EB;border-bottom:1px solid #E5E7EB;">
      {''.join(rows)}
    </table>
    """


def render_branded_email(
    *,
    title,
    body_html,
    button_text=None,
    button_url=None,
    info_rows=None,
    footer_note=None,
    subtitle="在地外送協作平台",
):
    title = html.escape(_safe_str(title))
    subtitle = html.escape(_safe_str(subtitle))
    button_text = html.escape(_safe_str(button_text))
    button_url = absolute_url(button_url)
    footer_note = footer_note or "此信件由 FUMAP GO 系統自動發送，請勿直接回覆。"

    cta_html = ""

    if button_text and button_url:
        cta_html = f"""
        <div style="margin:24px 0 8px 0;text-align:center;">
          <a href="{html.escape(button_url)}"
             style="display:inline-block;background:#06C755;color:#ffffff;text-decoration:none;
                    padding:13px 22px;border-radius:12px;font-size:16px;font-weight:700;">
            {button_text}
          </a>
        </div>
        <p style="font-size:12px;line-height:1.6;color:#6B7280;word-break:break-all;margin:12px 0 0 0;">
          如果按鈕無法開啟，請複製此連結：<br>
          <span style="color:#0B4EA2;">{html.escape(button_url)}</span>
        </p>
        """

    info_html = _info_rows_html(info_rows)

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#F4F8FB;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,'Noto Sans TC',sans-serif;">
  <div style="background:#F4F8FB;padding:24px 12px;">
    <div style="max-width:580px;margin:0 auto;background:#ffffff;border-radius:18px;
                overflow:hidden;border:1px solid #E5E7EB;box-shadow:0 8px 24px rgba(15,23,42,0.06);">
      <div style="background:#0B4EA2;padding:22px 24px;color:#ffffff;">
        <div style="font-size:26px;font-weight:800;letter-spacing:0.5px;">
          FUMAP GO
        </div>
        <div style="font-size:14px;opacity:0.92;margin-top:4px;">
          {subtitle}
        </div>
      </div>

      <div style="padding:26px 24px;">
        <h1 style="font-size:22px;line-height:1.35;color:#1F2937;margin:0 0 16px 0;">
          {title}
        </h1>

        <div style="font-size:15px;line-height:1.8;color:#374151;margin:0;">
          {body_html}
        </div>

        {info_html}
        {cta_html}
      </div>

      <div style="background:#F9FAFB;border-top:1px solid #E5E7EB;padding:18px 24px;">
        <p style="font-size:12px;line-height:1.7;color:#6B7280;margin:0;">
          {html.escape(_safe_str(footer_note))}<br>
          FUMAP GO｜fumapgo.com
        </p>
      </div>
    </div>
  </div>
</body>
</html>
"""


def html_to_text(html_body):
    if not html_body:
        return ""

    text = html_body
    replacements = [
        ("<br>", "\n"),
        ("<br/>", "\n"),
        ("<br />", "\n"),
        ("</p>", "\n\n"),
        ("</div>", "\n"),
        ("</h1>", "\n\n"),
        ("</h2>", "\n\n"),
        ("</li>", "\n"),
    ]

    for old, new in replacements:
        text = text.replace(old, new)

    inside_tag = False
    out = []

    for char in text:
        if char == "<":
            inside_tag = True
            continue

        if char == ">":
            inside_tag = False
            continue

        if not inside_tag:
            out.append(char)

    return html.unescape("".join(out)).strip()


def log_email(
    *,
    event_type=None,
    recipient_email=None,
    recipient_role=None,
    user_id=None,
    order_id=None,
    order_code=None,
    subject=None,
    status=None,
    error_message="",
    provider_message_id=None,
    retry_count=0,
):
    try:
        db = get_db()

        db.execute(
            """
            INSERT INTO email_logs (
                event_type,
                recipient_email,
                recipient_role,
                user_id,
                order_id,
                order_code,
                subject,
                status,
                error_message,
                provider_message_id,
                retry_count,
                last_attempt_at,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+8 hours'), datetime('now', '+8 hours'))
            """,
            (
                event_type or "",
                normalize_email(recipient_email),
                recipient_role or "",
                user_id,
                order_id,
                order_code or "",
                subject or "",
                status or "",
                (error_message or "")[:500],
                provider_message_id or "",
                int(retry_count or 0),
            ),
        )
        db.commit()

    except Exception as exc:
        print(f"[EMAIL][LOG][ERROR] failed to write email_logs: {exc}")


def send_email(
    *,
    to_email,
    subject,
    html_body=None,
    text_body=None,
    body=None,
    event_type=None,
    recipient_role=None,
    user_id=None,
    order_id=None,
    order_code=None,
):
    to_email = normalize_email(to_email)
    subject = subject or ""

    if body and not text_body and not html_body:
        text_body = body

    if html_body and not text_body:
        text_body = html_to_text(html_body)

    if not text_body:
        text_body = ""

    if not to_email:
        log_email(
            event_type=event_type,
            recipient_email=to_email,
            recipient_role=recipient_role,
            user_id=user_id,
            order_id=order_id,
            order_code=order_code,
            subject=subject,
            status=EMAIL_STATUS_SKIPPED,
            error_message="Missing recipient email",
        )
        return False

    if not is_valid_email(to_email):
        log_email(
            event_type=event_type,
            recipient_email=to_email,
            recipient_role=recipient_role,
            user_id=user_id,
            order_id=order_id,
            order_code=order_code,
            subject=subject,
            status=EMAIL_STATUS_SKIPPED,
            error_message="Invalid recipient email",
        )
        return False

    cfg = _smtp_config()

    if not smtp_is_configured():
        print("[EMAIL][WARN] SMTP not configured. Email skipped.")
        log_email(
            event_type=event_type,
            recipient_email=to_email,
            recipient_role=recipient_role,
            user_id=user_id,
            order_id=order_id,
            order_code=order_code,
            subject=subject,
            status=EMAIL_STATUS_SKIPPED,
            error_message="SMTP not configured",
        )
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_email"]
    msg["To"] = to_email
    msg.set_content(text_body)

    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=12) as smtp:
            if cfg["use_tls"]:
                smtp.starttls()

            smtp.login(cfg["username"], cfg["password"])
            smtp.send_message(msg)

        log_email(
            event_type=event_type,
            recipient_email=to_email,
            recipient_role=recipient_role,
            user_id=user_id,
            order_id=order_id,
            order_code=order_code,
            subject=subject,
            status=EMAIL_STATUS_SENT,
        )
        return True

    except Exception as exc:
        print(f"[EMAIL][ERROR] failed to send email: {exc}")
        log_email(
            event_type=event_type,
            recipient_email=to_email,
            recipient_role=recipient_role,
            user_id=user_id,
            order_id=order_id,
            order_code=order_code,
            subject=subject,
            status=EMAIL_STATUS_FAILED,
            error_message=str(exc),
        )
        return False

def send_email_verification(user_or_email, verify_url, user_id=None):
    if isinstance(user_or_email, str):
        to_email = normalize_email(user_or_email)
        target_user_id = user_id
    else:
        to_email = normalize_email(_row_get(user_or_email, "email", ""))
        target_user_id = user_id or _row_get(user_or_email, "id", None)

    subject = "FUMAP GO｜請確認您的 Email"

    body_html = """
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      感謝您註冊 FUMAP GO。
    </p>
    <p style="margin:0 0 12px 0;">
      請點擊下方按鈕確認您的 Email。
    </p>
    <p style="margin:0;color:#6B7280;">
      此連結將於 24 小時後失效。<br>
      如果不是您本人操作，請忽略此封信。
    </p>
    """

    html_body = render_branded_email(
        title="確認您的 FUMAP GO Email",
        body_html=body_html,
        button_text="確認 Email",
        button_url=verify_url,
    )

    text_body = f"""您好，

感謝您註冊 FUMAP GO。
請點擊以下連結確認您的 Email：

{absolute_url(verify_url)}

此連結將於 24 小時後失效。
如果不是您本人操作，請忽略此封信。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="EMAIL_VERIFICATION_REQUESTED",
        recipient_role="USER",
        user_id=target_user_id,
    )


def send_password_reset_email(to_email, reset_url, user_id=None):
    subject = "FUMAP GO｜密碼重設連結"

    body_html = """
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      我們收到您的 FUMAP GO 密碼重設申請。
    </p>
    <p style="margin:0 0 12px 0;">
      請點擊下方按鈕重新設定密碼。
    </p>
    <p style="margin:0;color:#6B7280;">
      此連結將於 30 分鐘後失效。<br>
      如果不是您本人操作，請忽略此封信。
    </p>
    """

    html_body = render_branded_email(
        title="重設您的 FUMAP GO 密碼",
        body_html=body_html,
        button_text="重設密碼",
        button_url=reset_url,
    )

    text_body = f"""您好，

我們收到您的 FUMAP GO 密碼重設申請。

請點擊以下連結重新設定密碼：
{absolute_url(reset_url)}

此連結將於 30 分鐘後失效。
如果不是您本人操作，請忽略此封信。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="PASSWORD_RESET_REQUESTED",
        recipient_role="USER",
        user_id=user_id,
    )


def send_test_email(to_email=None):
    target = normalize_email(to_email)

    if not target:
        admins = get_admin_notify_emails()
        target = admins[0] if admins else ""

    body_html = """
    <p style="margin:0 0 12px 0;">這是一封 FUMAP GO 測試信。</p>
    <p style="margin:0;color:#6B7280;">
      如果您收到此信，代表 SMTP、品牌 Email Template 與 email_logs 基本功能正常。
    </p>
    """

    html_body = render_branded_email(
        title="FUMAP GO Email 測試",
        body_html=body_html,
        button_text="前往 FUMAP GO",
        button_url=current_app.config.get("APP_BASE_URL") or current_app.config.get("PUBLIC_BASE_URL") or "/",
        info_rows=[
            ("系統", "FUMAP GO"),
            ("事件", "EMAIL_TEST"),
        ],
    )

    text_body = """FUMAP GO Email 測試

這是一封 FUMAP GO 測試信。
如果您收到此信，代表 SMTP、品牌 Email Template 與 email_logs 基本功能正常。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=target,
        subject="FUMAP GO｜Email 測試",
        html_body=html_body,
        text_body=text_body,
        event_type="EMAIL_TEST",
        recipient_role="ADMIN",
    )


def send_line_bind_success_email(
    to_email,
    *,
    role="",
    role_label="",
    target_code="",
    line_display_name="",
    user_id=None,
):
    to_email = normalize_email(to_email)
    role = _safe_str(role)
    role_label = _safe_str(role_label or role)
    target_code = _safe_str(target_code)
    line_display_name = _safe_str(line_display_name)

    subject = "FUMAP GO｜LINE 綁定成功"

    body_html = """
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      您的 FUMAP GO 帳號已成功綁定 LINE。
    </p>
    <p style="margin:0 0 12px 0;">
      之後重要通知會同時透過 Email 與 LINE 發送。
    </p>
    <p style="margin:0;color:#6B7280;">
      LINE 綁定只用於通知，不是登入方式，也不會取代 Email。
    </p>
    """

    html_body = render_branded_email(
        title="LINE 綁定成功",
        body_html=body_html,
        button_text="查看 LINE 綁定狀態",
        button_url="/line/bind",
        info_rows=[
            ("角色", role_label or role or "-"),
            ("帳號代碼", target_code or "-"),
            ("LINE 名稱", line_display_name or "-"),
            ("通知方式", "Email + LINE"),
        ],
    )

    text_body = f"""您好，

您的 FUMAP GO 帳號已成功綁定 LINE。
之後重要通知會同時透過 Email 與 LINE 發送。

角色：{role_label or role or '-'}
帳號代碼：{target_code or '-'}
LINE 名稱：{line_display_name or '-'}

LINE 綁定只用於通知，不是登入方式，也不會取代 Email。

查看 LINE 綁定狀態：
{absolute_url('/line/bind')}

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="LINE_BIND_SUCCESS_EMAIL",
        recipient_role=role or "USER",
        user_id=user_id,
    )


def _customer_order_url(order, order_url=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    guest_token = _safe_str(_order_value(order, "guest_access_token", ""))

    if order_url:
        return absolute_url(order_url)

    if guest_token:
        return absolute_url(f"/guest/orders/{order_code}?token={guest_token}")

    return absolute_url(f"/orders?order_code={order_code}")


def _admin_order_url(order, review_url=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    return absolute_url(review_url or f"/admin/orders?order_code={order_code}")


def _store_order_url(order, store_url=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    return absolute_url(store_url or f"/store/orders?order_code={order_code}")


def _order_info_rows(order, extra_rows=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    store_name = _safe_str(
        _order_value(order, "store_name", "")
        or _order_value(order, "manual_order_title", "")
        or _order_value(order, "store_title", "")
    )
    customer_name = _safe_str(_order_value(order, "customer_name", ""))
    customer_email = _safe_str(_order_value(order, "customer_email", ""))
    total_twd = _format_twd(_order_value(order, "total_twd", 0))
    payment_method = _safe_str(_order_value(order, "payment_method", ""))
    payment_status = _safe_str(_order_value(order, "payment_status", ""))

    rows = [
        ("訂單編號", order_code or "UNKNOWN"),
        ("店家名稱", store_name or "-"),
        ("客戶姓名", customer_name or "-"),
        ("客戶 Email", customer_email or "-"),
        ("訂單金額", total_twd),
        ("付款方式", payment_method or "-"),
        ("付款狀態", payment_status or "-"),
    ]

    if extra_rows:
        rows.extend(extra_rows)

    return rows


def _send_to_admins(*, subject, html_body, text_body, event_type, order=None):
    order_id = _order_value(order, "id", None)
    order_code = _safe_str(_order_value(order, "order_code", ""))
    admin_emails = get_admin_notify_emails()

    if not admin_emails:
        log_email(
            event_type=event_type,
            recipient_email="",
            recipient_role="ADMIN",
            order_id=order_id,
            order_code=order_code,
            subject=subject,
            status=EMAIL_STATUS_SKIPPED,
            error_message="No admin notification email configured",
        )
        return False

    sent_any = False
    seen = set()

    for admin_email in admin_emails:
        admin_email = normalize_email(admin_email)

        if not admin_email or admin_email in seen:
            continue

        seen.add(admin_email)

        sent = send_email(
            to_email=admin_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            event_type=event_type,
            recipient_role="ADMIN",
            order_id=order_id,
            order_code=order_code,
        )

        if sent:
            sent_any = True

    return sent_any


def send_customer_order_created_email(order, customer_email=None, order_url=None):
    customer_email = normalize_email(
        customer_email
        or _order_value(order, "customer_email", "")
    )

    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)
    order_source = _safe_str(_order_value(order, "order_source", ""))
    guest_token = _safe_str(_order_value(order, "guest_access_token", ""))

    if not customer_email:
        log_email(
            event_type="CUSTOMER_ORDER_CREATED",
            recipient_email="",
            recipient_role="CUSTOMER",
            user_id=_order_value(order, "customer_user_id", None),
            order_id=order_id,
            order_code=order_code,
            subject=f"FUMAP GO｜訂單已建立｜{order_code or 'UNKNOWN'}",
            status=EMAIL_STATUS_SKIPPED,
            error_message="Missing customer email",
        )
        return False

    final_order_url = _customer_order_url(order, order_url)

    title = "訂單已建立"
    subject = f"FUMAP GO｜訂單已建立｜{order_code or 'UNKNOWN'}"

    body_html = """
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      您的 FUMAP GO 訂單已建立。
    </p>
    <p style="margin:0 0 12px 0;">
      Email 是主要通知方式。若您有綁定 LINE，系統會額外推播 LINE 通知。
    </p>
    """

    if guest_token:
        body_html += """
        <p style="margin:0;color:#6B7280;">
          此訂單為訪客訂單，請保留下方訂單追蹤連結。
        </p>
        """

    html_body = render_branded_email(
        title=title,
        body_html=body_html,
        button_text="查看訂單",
        button_url=final_order_url,
        info_rows=_order_info_rows(
            order,
            [
                ("訂單來源", order_source or "-"),
                ("通知方式", "Email 為主；LINE 為額外推播"),
            ],
        ),
    )

    text_body = f"""您好，

您的 FUMAP GO 訂單已建立。
Email 是主要通知方式。若您有綁定 LINE，系統會額外推播 LINE 通知。

訂單編號：{order_code or 'UNKNOWN'}
店家：{_safe_str(_order_value(order, 'store_name', '-')) or '-'}
金額：{_format_twd(_order_value(order, 'total_twd', 0))}
付款：{_safe_str(_order_value(order, 'payment_method', '-')) or '-'} / {_safe_str(_order_value(order, 'payment_status', '-')) or '-'}

查看訂單：
{final_order_url}

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=customer_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="CUSTOMER_ORDER_CREATED",
        recipient_role="CUSTOMER",
        user_id=_order_value(order, "customer_user_id", None),
        order_id=order_id,
        order_code=order_code,
    )


def send_admin_order_created_email(order, review_url=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    admin_order_url = _admin_order_url(order, review_url)

    subject = f"FUMAP GO｜新訂單已建立｜{order_code or 'UNKNOWN'}"

    body_html = """
    <p style="margin:0 0 12px 0;">Admin 您好，</p>
    <p style="margin:0 0 12px 0;">
      系統收到一筆新的客戶訂單。
    </p>
    <p style="margin:0;color:#6B7280;">
      Email 是客戶主要通知方式；LINE 只作為額外推播，不應阻擋訂單建立。
    </p>
    """

    html_body = render_branded_email(
        title="新訂單已建立",
        body_html=body_html,
        button_text="查看 Admin 訂單",
        button_url=admin_order_url,
        info_rows=_order_info_rows(
            order,
            [
                ("訂單來源", _safe_str(_order_value(order, "order_source", "")) or "-"),
                ("客戶電話", _safe_str(_order_value(order, "customer_phone", "")) or "-"),
                ("配送地址", _safe_str(_order_value(order, "delivery_address", "")) or "-"),
            ],
        ),
    )

    text_body = f"""Admin 您好，

系統收到一筆新的客戶訂單。

訂單編號：{order_code or 'UNKNOWN'}
客戶：{_safe_str(_order_value(order, 'customer_name', '-')) or '-'}
電話：{_safe_str(_order_value(order, 'customer_phone', '-')) or '-'}
Email：{_safe_str(_order_value(order, 'customer_email', '-')) or '-'}
店家：{_safe_str(_order_value(order, 'store_name', '-')) or '-'}
金額：{_format_twd(_order_value(order, 'total_twd', 0))}
付款：{_safe_str(_order_value(order, 'payment_method', '-')) or '-'} / {_safe_str(_order_value(order, 'payment_status', '-')) or '-'}

查看 Admin 訂單：
{admin_order_url}

FUMAP GO
fumapgo.com
"""

    return _send_to_admins(
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="ADMIN_ORDER_CREATED",
        order=order,
    )

def send_customer_payment_proof_received_email(order, customer_email, order_url=None):
    customer_email = normalize_email(customer_email)
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)
    uploaded_at = _safe_str(_order_value(order, "payment_proof_uploaded_at", ""))

    subject = f"FUMAP GO｜已收到您的轉帳證明｜{order_code or 'UNKNOWN'}"

    extra_rows = [
        ("付款證明狀態", _safe_str(_order_value(order, "payment_proof_status", "PENDING_REVIEW")) or "PENDING_REVIEW"),
    ]

    if uploaded_at:
        extra_rows.append(("上傳時間", uploaded_at))

    final_order_url = _customer_order_url(order, order_url)

    body_html = """
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      系統已收到您的轉帳付款證明。
    </p>
    <p style="margin:0 0 12px 0;">
      目前狀態為等待 Admin 確認。確認完成後，系統會再通知您。
    </p>
    <p style="margin:0;color:#6B7280;">
      為了保護隱私，V1 不會將圖片作為 Email 附件寄出。
    </p>
    """

    html_body = render_branded_email(
        title="已收到您的轉帳證明",
        body_html=body_html,
        button_text="查看訂單",
        button_url=final_order_url,
        info_rows=_order_info_rows(order, extra_rows),
    )

    text_body = f"""您好，

系統已收到您的轉帳付款證明。
目前狀態為等待 Admin 確認。確認完成後，系統會再通知您。

訂單編號：{order_code or 'UNKNOWN'}
付款狀態：{_safe_str(_order_value(order, 'payment_status', '-')) or '-'}
上傳時間：{uploaded_at or '-'}

查看訂單：
{final_order_url}

為了保護隱私，V1 不會將圖片作為 Email 附件寄出。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=customer_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="CUSTOMER_PAYMENT_PROOF_RECEIVED",
        recipient_role="CUSTOMER",
        user_id=_order_value(order, "customer_user_id", None),
        order_id=order_id,
        order_code=order_code,
    )


def send_admin_payment_proof_uploaded_email(order, admin_emails=None, review_url=None, proof_url=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)

    subject = f"FUMAP GO｜有新的轉帳付款證明等待確認｜訂單 {order_code or 'UNKNOWN'}"

    customer_name = _safe_str(_order_value(order, "customer_name", ""))
    customer_phone = _safe_str(_order_value(order, "customer_phone", ""))
    customer_email = _safe_str(_order_value(order, "customer_email", ""))
    store_name = _safe_str(_order_value(order, "store_name", ""))
    total_twd = _format_twd(_order_value(order, "total_twd", 0))
    payment_method = _safe_str(_order_value(order, "payment_method", ""))
    payment_status = _safe_str(_order_value(order, "payment_status", ""))
    uploaded_at = _safe_str(_order_value(order, "payment_proof_uploaded_at", ""))
    proof_status = _safe_str(_order_value(order, "payment_proof_status", "PENDING_REVIEW"))

    admin_order_url = _admin_order_url(order, review_url)
    proof_url = absolute_url(
        proof_url
        or _order_value(order, "payment_proof_image_url", "")
        or admin_order_url
    )

    info_rows = [
        ("訂單編號", order_code or "UNKNOWN"),
        ("客戶姓名", customer_name or "-"),
        ("客戶電話", customer_phone or "-"),
        ("客戶 Email", customer_email or "-"),
        ("店家名稱", store_name or "-"),
        ("訂單金額", total_twd),
        ("付款方式", payment_method or "-"),
        ("付款狀態", payment_status or "-"),
        ("付款證明狀態", proof_status or "PENDING_REVIEW"),
    ]

    if uploaded_at:
        info_rows.append(("上傳時間", uploaded_at))

    body_html = """
    <p style="margin:0 0 12px 0;">Admin 您好，</p>
    <p style="margin:0 0 12px 0;">
      系統收到一筆新的轉帳付款證明，請至後台查看並確認付款。
    </p>
    <p style="margin:0;color:#6B7280;">
      V1 不會將圖片作為附件寄出，請在系統內查看，以降低隱私與寄送失敗風險。
    </p>
    """

    html_body = render_branded_email(
        title="有新的轉帳付款證明等待確認",
        body_html=body_html,
        button_text="前往 Admin 確認",
        button_url=admin_order_url,
        info_rows=info_rows,
    )

    text_body = f"""Admin 您好，

系統收到一筆新的轉帳付款證明，請至後台查看並確認付款。

訂單編號：{order_code or 'UNKNOWN'}
客戶姓名：{customer_name or '-'}
客戶電話：{customer_phone or '-'}
客戶 Email：{customer_email or '-'}
店家名稱：{store_name or '-'}
訂單金額：{total_twd}
付款方式：{payment_method or '-'}
付款狀態：{payment_status or '-'}
付款證明狀態：{proof_status or 'PENDING_REVIEW'}
上傳時間：{uploaded_at or '-'}

查看訂單：
{admin_order_url}

付款證明連結：
{proof_url}

FUMAP GO
fumapgo.com
"""

    if admin_emails is None:
        admin_emails = get_admin_notify_emails()

    if isinstance(admin_emails, str):
        admin_emails = [admin_emails]

    normalized_admin_emails = []
    seen = set()

    for email_addr in admin_emails or []:
        email_addr = normalize_email(email_addr)

        if not email_addr or email_addr in seen:
            continue

        seen.add(email_addr)
        normalized_admin_emails.append(email_addr)

    if not normalized_admin_emails:
        log_email(
            event_type="ADMIN_PAYMENT_PROOF_UPLOADED",
            recipient_email="",
            recipient_role="ADMIN",
            order_id=order_id,
            order_code=order_code,
            subject=subject,
            status=EMAIL_STATUS_SKIPPED,
            error_message="No admin notification email configured",
        )
        return False

    sent_any = False

    for admin_email in normalized_admin_emails:
        sent = send_email(
            to_email=admin_email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            event_type="ADMIN_PAYMENT_PROOF_UPLOADED",
            recipient_role="ADMIN",
            order_id=order_id,
            order_code=order_code,
        )

        if sent:
            sent_any = True

    return sent_any


def send_admin_payment_proof_email(order, proof_url=None, admin_order_url=None):
    return send_admin_payment_proof_uploaded_email(
        order,
        admin_emails=None,
        review_url=admin_order_url,
        proof_url=proof_url,
    )


def send_customer_payment_verified_email(order, customer_email, order_url=None):
    customer_email = normalize_email(customer_email)
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)
    verified_at = _safe_str(_order_value(order, "payment_verified_at", ""))

    subject = f"FUMAP GO｜付款已確認｜{order_code or 'UNKNOWN'}"
    final_order_url = _customer_order_url(order, order_url)

    body_html = """
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      您的轉帳付款已由 Admin 確認。
    </p>
    <p style="margin:0 0 12px 0;">
      店家將依訂單流程開始處理，您可以在訂單頁查看最新狀態。
    </p>
    """

    html_body = render_branded_email(
        title="付款已確認",
        body_html=body_html,
        button_text="查看訂單",
        button_url=final_order_url,
        info_rows=_order_info_rows(order, [("確認時間", verified_at or "-")]),
    )

    text_body = f"""您好，

您的轉帳付款已由 Admin 確認。
店家將依訂單流程開始處理，您可以在訂單頁查看最新狀態。

訂單編號：{order_code or 'UNKNOWN'}
確認時間：{verified_at or '-'}

查看訂單：
{final_order_url}

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=customer_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="CUSTOMER_PAYMENT_VERIFIED",
        recipient_role="CUSTOMER",
        user_id=_order_value(order, "customer_user_id", None),
        order_id=order_id,
        order_code=order_code,
    )


def send_customer_payment_rejected_email(order, customer_email, reason="", order_url=None):
    customer_email = normalize_email(customer_email)
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)
    reject_reason = _safe_str(reason or _order_value(order, "payment_reject_reason", "")) or "轉帳付款證明需要重新確認"
    final_order_url = _customer_order_url(order, order_url)

    subject = f"FUMAP GO｜轉帳證明需要重新確認｜{order_code or 'UNKNOWN'}"

    body_html = f"""
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      您的轉帳付款證明需要重新確認。
    </p>
    <p style="margin:0 0 12px 0;">
      原因：<b>{html.escape(reject_reason)}</b>
    </p>
    <p style="margin:0;color:#6B7280;">
      請查看訂單狀態，必要時重新上傳付款證明或聯絡 Admin。
    </p>
    """

    html_body = render_branded_email(
        title="轉帳證明需要重新確認",
        body_html=body_html,
        button_text="查看訂單",
        button_url=final_order_url,
        info_rows=_order_info_rows(order, [("原因", reject_reason)]),
    )

    text_body = f"""您好，

您的轉帳付款證明需要重新確認。

訂單編號：{order_code or 'UNKNOWN'}
原因：{reject_reason}

請查看訂單狀態，必要時重新上傳付款證明或聯絡 Admin。

查看訂單：
{final_order_url}

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=customer_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="CUSTOMER_PAYMENT_REJECTED",
        recipient_role="CUSTOMER",
        user_id=_order_value(order, "customer_user_id", None),
        order_id=order_id,
        order_code=order_code,
    )


def send_customer_delivery_proof_email(order, customer_email, order_url=None):
    customer_email = normalize_email(customer_email)
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)
    final_order_url = _customer_order_url(order, order_url)

    subject = f"FUMAP GO｜訂單已送達｜{order_code or 'UNKNOWN'}"

    body_html = """
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      您的 FUMAP GO 訂單已送達。
    </p>
    <p style="margin:0;color:#6B7280;">
      若有配送證明，系統會保存於訂單紀錄中。為了保護隱私，V1 不會將圖片作為附件寄出。
    </p>
    """

    html_body = render_branded_email(
        title="訂單已送達",
        body_html=body_html,
        button_text="查看訂單狀態",
        button_url=final_order_url,
        info_rows=_order_info_rows(order),
    )

    text_body = f"""您好，

您的 FUMAP GO 訂單已送達。

訂單編號：{order_code or 'UNKNOWN'}
店家：{_safe_str(_order_value(order, 'store_name', '-')) or '-'}
狀態：{_safe_str(_order_value(order, 'status', '-')) or '-'}

查看訂單狀態：
{final_order_url}

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=customer_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="CUSTOMER_DELIVERY_PROOF",
        recipient_role="CUSTOMER",
        user_id=_order_value(order, "customer_user_id", None),
        order_id=order_id,
        order_code=order_code,
    )

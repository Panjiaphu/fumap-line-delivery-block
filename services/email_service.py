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
            return row[key]
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
      LINE 綁定只用於通知，不是登入方式，也不會取代 Email 驗證。
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

LINE 綁定只用於通知，不是登入方式，也不會取代 Email 驗證。

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

def send_admin_line_bind_success_email(
    *,
    user,
    role="",
    role_label="",
    target_code="",
    line_display_name="",
    contact_code="",
    admin_emails=None,
):
    """
    Notify Admin by Email after a user successfully binds LINE.

    Rules:
    - Uses ADMIN_NOTIFY_EMAILS / ADMIN_NOTIFY_EMAIL when admin_emails is None.
    - Failure/skipped email must not rollback LINE bind.
    - No LINE permission or login permission is granted by this email.
    """
    role = _safe_str(role)
    role_label = _safe_str(role_label or role)
    target_code = _safe_str(target_code)
    line_display_name = _safe_str(line_display_name)
    contact_code = _safe_str(contact_code)

    try:
        user_id = _row_get(user, "id", None)
        user_email = normalize_email(_row_get(user, "email", ""))
        user_phone = _safe_str(_row_get(user, "phone", ""))
        user_name = _safe_str(
            _row_get(user, "display_name", "")
            or _row_get(user, "login_id", "")
        )
    except Exception:
        user_id = None
        user_email = ""
        user_phone = ""
        user_name = ""

    if admin_emails is None:
        admin_emails = get_admin_notify_emails()

    if isinstance(admin_emails, str):
        admin_emails = [admin_emails]

    subject = "FUMAP GO｜新的 LINE 綁定成功"

    body_html = """
    <p style="margin:0 0 12px 0;">Admin 您好，</p>
    <p style="margin:0 0 12px 0;">
      有一個 FUMAP GO 帳號完成 LINE 綁定。
    </p>
    <p style="margin:0 0 12px 0;">
      之後此帳號可接收 LINE 通知；LINE 綁定僅作為通知管道，不代表登入授權或帳號權限變更。
    </p>
    """

    html_body = render_branded_email(
        title="新的 LINE 綁定成功",
        body_html=body_html,
        button_text="查看 LINE 綁定狀態",
        button_url="/line/contacts",
        info_rows=[
            ("角色", role_label or role or "-"),
            ("帳號代碼", target_code or "-"),
            ("LINE 名稱", line_display_name or "-"),
            ("LINE 通知代碼", contact_code or "-"),
            ("帳號名稱", user_name or "-"),
            ("Email", user_email or "-"),
            ("電話", user_phone or "-"),
            ("通知方式", "Admin Email + Admin LINE"),
        ],
    )

    text_body = f"""Admin 您好，

有一個 FUMAP GO 帳號完成 LINE 綁定。

角色：{role_label or role or '-'}
帳號代碼：{target_code or '-'}
LINE 名稱：{line_display_name or '-'}
LINE 通知代碼：{contact_code or '-'}
帳號名稱：{user_name or '-'}
Email：{user_email or '-'}
電話：{user_phone or '-'}

LINE 綁定僅作為通知管道，不代表登入授權或帳號權限變更。

查看 LINE 綁定狀態：
{absolute_url('/line/contacts')}

FUMAP GO
fumapgo.com
"""

    sent_any = False

    for email in admin_emails or []:
        email = normalize_email(email)

        if not email:
            continue

        sent = send_email(
            to_email=email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            event_type="LINE_BIND_SUCCESS_ADMIN_EMAIL",
            recipient_role="ADMIN",
            user_id=user_id,
        )

        if sent:
            sent_any = True

    return sent_any

def _customer_order_url(order, order_url=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    return absolute_url(order_url or f"/orders?order_code={order_code}")


def _admin_order_url(order, review_url=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    return absolute_url(review_url or f"/admin/orders?order_code={order_code}")


def _order_info_rows(order, extra_rows=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    store_name = _safe_str(
        _order_value(order, "store_name", "")
        or _order_value(order, "manual_order_title", "")
        or _order_value(order, "store_title", "")
    )
    customer_name = _safe_str(_order_value(order, "customer_name", ""))
    total_twd = _format_twd(_order_value(order, "total_twd", 0))
    payment_method = _safe_str(_order_value(order, "payment_method", ""))
    payment_status = _safe_str(_order_value(order, "payment_status", ""))

    rows = [
        ("訂單編號", order_code or "UNKNOWN"),
        ("店家名稱", store_name or "-"),
        ("客戶姓名", customer_name or "-"),
        ("訂單金額", total_twd),
        ("付款方式", payment_method or "-"),
        ("付款狀態", payment_status or "-"),
    ]

    if extra_rows:
        rows.extend(extra_rows)

    return rows


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

def send_customer_payment_verified_email(order, customer_email, order_url=None):
    customer_email = normalize_email(customer_email)
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)
    verified_at = _safe_str(_order_value(order, "payment_verified_at", ""))

    subject = f"FUMAP GO｜付款已確認｜{order_code or 'UNKNOWN'}"

    extra_rows = [("確認結果", "付款已確認")]

    if verified_at:
        extra_rows.append(("確認時間", verified_at))

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
        info_rows=_order_info_rows(order, extra_rows),
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
    rejected_at = _safe_str(_order_value(order, "payment_rejected_at", ""))
    reject_reason = _safe_str(reason or _order_value(order, "payment_reject_reason", "")) or "轉帳付款證明需要重新確認"

    subject = f"FUMAP GO｜轉帳證明需要重新確認｜{order_code or 'UNKNOWN'}"

    extra_rows = [
        ("確認結果", "需要重新確認"),
        ("原因", reject_reason),
    ]

    if rejected_at:
        extra_rows.append(("處理時間", rejected_at))

    final_order_url = _customer_order_url(order, order_url)

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
        info_rows=_order_info_rows(order, extra_rows),
    )

    text_body = f"""您好，

您的轉帳付款證明需要重新確認。

訂單編號：{order_code or 'UNKNOWN'}
原因：{reject_reason}
處理時間：{rejected_at or '-'}

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

def send_store_payment_verified_email(order, store_email, order_url=None):
    store_email = normalize_email(store_email)
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)

    subject = f"FUMAP GO｜轉帳付款已確認，請處理訂單｜{order_code or 'UNKNOWN'}"

    final_order_url = absolute_url(order_url or f"/store/orders/{order_code}")

    body_html = """
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      此訂單的轉帳付款已由 Admin 確認。
    </p>
    <p style="margin:0 0 12px 0;">
      系統已解除付款暫停，店家可以依訂單流程處理商品、安排出餐或呼叫 Shiper。
    </p>
    <p style="margin:0;color:#6B7280;">
      請登入店家工作台查看訂單狀態。此通知不代表銀行即時入帳，只代表 Admin 已完成系統確認。
    </p>
    """

    html_body = render_branded_email(
        title="轉帳付款已確認",
        body_html=body_html,
        button_text="查看店家訂單",
        button_url=final_order_url,
        info_rows=_order_info_rows(
            order,
            extra_rows=[
                ("Admin 確認結果", "付款已確認"),
                ("店家處理建議", "可以開始處理訂單"),
            ],
        ),
    )

    text_body = f"""您好，

此訂單的轉帳付款已由 Admin 確認。
系統已解除付款暫停，店家可以依訂單流程處理商品、安排出餐或呼叫 Shiper。

訂單編號：{order_code or 'UNKNOWN'}
付款狀態：{_safe_str(_order_value(order, 'payment_status', '-')) or '-'}
付款方式：{_safe_str(_order_value(order, 'payment_method', '-')) or '-'}

查看店家訂單：
{final_order_url}

此通知不代表銀行即時入帳，只代表 Admin 已完成系統確認。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=store_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="STORE_PAYMENT_VERIFIED",
        recipient_role="STORE",
        order_id=order_id,
        order_code=order_code,
    )


def send_store_payment_rejected_email(order, store_email, reason="", order_url=None):
    store_email = normalize_email(store_email)
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)
    reject_reason = _safe_str(reason or _order_value(order, "payment_reject_reason", "")) or "轉帳付款證明需要重新確認"

    subject = f"FUMAP GO｜轉帳證明未通過，訂單仍暫停｜{order_code or 'UNKNOWN'}"

    final_order_url = absolute_url(order_url or f"/store/orders/{order_code}")

    body_html = f"""
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      此訂單的轉帳付款證明未通過 Admin 確認。
    </p>
    <p style="margin:0 0 12px 0;">
      原因：<b>{html.escape(reject_reason)}</b>
    </p>
    <p style="margin:0 0 12px 0;">
      此訂單目前仍為暫停狀態，請先不要製作商品或呼叫 Shiper，等待客戶重新上傳付款證明並由 Admin 再次確認。
    </p>
    <p style="margin:0;color:#6B7280;">
      請登入店家工作台查看訂單狀態。
    </p>
    """

    html_body = render_branded_email(
        title="轉帳證明未通過",
        body_html=body_html,
        button_text="查看店家訂單",
        button_url=final_order_url,
        info_rows=_order_info_rows(
            order,
            extra_rows=[
                ("Admin 確認結果", "需要重新上傳"),
                ("原因", reject_reason),
                ("店家處理建議", "暫停處理訂單"),
            ],
        ),
    )

    text_body = f"""您好，

此訂單的轉帳付款證明未通過 Admin 確認。

訂單編號：{order_code or 'UNKNOWN'}
付款狀態：{_safe_str(_order_value(order, 'payment_status', '-')) or '-'}
付款方式：{_safe_str(_order_value(order, 'payment_method', '-')) or '-'}
原因：{reject_reason}

此訂單目前仍為暫停狀態，請先不要製作商品或呼叫 Shiper，等待客戶重新上傳付款證明並由 Admin 再次確認。

查看店家訂單：
{final_order_url}

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=store_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="STORE_PAYMENT_REJECTED",
        recipient_role="STORE",
        order_id=order_id,
        order_code=order_code,
    )

def send_admin_payment_proof_uploaded_email(order, admin_emails=None, review_url=None, proof_url=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)

    subject = f"FUMAP GO｜有新的轉帳付款證明等待確認｜訂單 {order_code or 'UNKNOWN'}"

    customer_name = _safe_str(_order_value(order, "customer_name", ""))
    customer_phone = _safe_str(_order_value(order, "customer_phone", ""))
    customer_email = _safe_str(
        _order_value(order, "customer_email", "")
        or _order_value(order, "email", "")
        or _order_value(order, "user_email", "")
    )
    store_name = _safe_str(
        _order_value(order, "store_name", "")
        or _order_value(order, "manual_order_title", "")
        or _order_value(order, "store_title", "")
    )
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
      V1C 不會將圖片作為附件寄出，請在系統內查看，以降低隱私與寄送失敗風險。
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

def _returned_to_store_common_rows(order):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    store_name = _safe_str(
        _order_value(order, "store_name", "")
        or _order_value(order, "manual_order_title", "")
        or _order_value(order, "store_title", "")
    )

    rows = [
        ("訂單編號", order_code or "UNKNOWN"),
        ("店家名稱", store_name or "-"),
        ("目前狀態", _safe_str(_order_value(order, "status", "RETURNED_TO_STORE")) or "RETURNED_TO_STORE"),
        ("付款方式", _safe_str(_order_value(order, "payment_method", "")) or "-"),
        ("付款狀態", _safe_str(_order_value(order, "payment_status", "")) or "-"),
        ("訂單金額", _format_twd(_order_value(order, "total_twd", 0))),
    ]

    returned_at = _safe_str(
        _order_value(order, "return_proof_uploaded_at", "")
        or _order_value(order, "updated_at", "")
    )

    if returned_at:
        rows.append(("退回確認時間", returned_at))

    return rows


def send_customer_returned_to_store_email(order, customer_email, order_url=None):
    customer_email = normalize_email(customer_email)
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)
    order_url = absolute_url(order_url or f"/orders?order_code={order_code}")

    subject = f"FUMAP GO｜訂單已退回店家處理｜{order_code or 'UNKNOWN'}"

    body_html = """
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      您的訂單因配送異常，Shiper 已將商品退回店家。
    </p>
    <p style="margin:0 0 12px 0;">
      後續退款、重新配送或爭議處理，會由店家與 Admin 依系統紀錄確認。
    </p>
    <p style="margin:0;color:#6B7280;">
      系統已保存退回證明圖片。為了保護隱私，Email 不會附加圖片檔案。
    </p>
    """

    html_body = render_branded_email(
        title="訂單已退回店家處理",
        body_html=body_html,
        button_text="查看訂單狀態",
        button_url=order_url,
        info_rows=_returned_to_store_common_rows(order),
    )

    text_body = f"""您好，

您的 FUMAP GO 訂單因配送異常，Shiper 已將商品退回店家。

訂單編號：{order_code or 'UNKNOWN'}
目前狀態：RETURNED_TO_STORE

後續退款、重新配送或爭議處理，會由店家與 Admin 依系統紀錄確認。

查看訂單狀態：
{order_url}

系統已保存退回證明圖片。為了保護隱私，Email 不會附加圖片檔案。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=customer_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="RETURNED_TO_STORE_CUSTOMER_EMAIL",
        recipient_role="CUSTOMER",
        order_id=order_id,
        order_code=order_code,
    )


def send_store_returned_to_store_email(order, store_email, order_url=None):
    store_email = normalize_email(store_email)
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)
    order_url = absolute_url(order_url or f"/store/orders/{order_code}")

    subject = f"FUMAP GO｜商品已退回店家｜{order_code or 'UNKNOWN'}"

    body_html = """
    <p style="margin:0 0 12px 0;">您好，</p>
    <p style="margin:0 0 12px 0;">
      此訂單已進入退回店家完成狀態。Shiper 已確認商品退回，系統已保存退回證明。
    </p>
    <p style="margin:0 0 12px 0;">
      請依實際情況與 Admin 確認後續對帳、退款、補送或爭議處理。
    </p>
    <p style="margin:0;color:#6B7280;">
      Email 不會附加圖片檔案，請登入系統查看訂單與證明紀錄。
    </p>
    """

    html_body = render_branded_email(
        title="商品已退回店家",
        body_html=body_html,
        button_text="查看店家訂單",
        button_url=order_url,
        info_rows=_returned_to_store_common_rows(order),
    )

    text_body = f"""您好，

此訂單已進入退回店家完成狀態。Shiper 已確認商品退回，系統已保存退回證明。

訂單編號：{order_code or 'UNKNOWN'}
目前狀態：RETURNED_TO_STORE

請依實際情況與 Admin 確認後續對帳、退款、補送或爭議處理。

查看店家訂單：
{order_url}

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=store_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="RETURNED_TO_STORE_STORE_EMAIL",
        recipient_role="STORE",
        order_id=order_id,
        order_code=order_code,
    )


def send_admin_returned_to_store_email(order, admin_emails=None, admin_order_url=None):
    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)
    admin_order_url = absolute_url(admin_order_url or f"/admin/orders/{order_code}")

    if admin_emails is None:
        admin_emails = get_admin_notify_emails()

    if isinstance(admin_emails, str):
        admin_emails = [admin_emails]

    subject = f"FUMAP GO｜退回店家待處理｜{order_code or 'UNKNOWN'}"

    body_html = """
    <p style="margin:0 0 12px 0;">Admin 您好，</p>
    <p style="margin:0 0 12px 0;">
      此訂單已由 Shiper 確認退回店家。請檢查退回證明、付款狀態與後續對帳 / 退款 / 爭議處理。
    </p>
    <p style="margin:0;color:#6B7280;">
      退回證明圖片已保存於系統，不會作為 Email 附件寄出。
    </p>
    """

    html_body = render_branded_email(
        title="退回店家待 Admin 處理",
        body_html=body_html,
        button_text="查看 Admin 訂單",
        button_url=admin_order_url,
        info_rows=_returned_to_store_common_rows(order),
    )

    text_body = f"""Admin 您好，

此訂單已由 Shiper 確認退回店家。請檢查退回證明、付款狀態與後續對帳 / 退款 / 爭議處理。

訂單編號：{order_code or 'UNKNOWN'}
目前狀態：RETURNED_TO_STORE

查看 Admin 訂單：
{admin_order_url}

FUMAP GO
fumapgo.com
"""

    sent_any = False

    for email in admin_emails or []:
        email = normalize_email(email)

        if not email:
            continue

        sent = send_email(
            to_email=email,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            event_type="RETURNED_TO_STORE_ADMIN_EMAIL",
            recipient_role="ADMIN",
            order_id=order_id,
            order_code=order_code,
        )

        if sent:
            sent_any = True

    return sent_any

def send_customer_delivery_proof_email(order, customer_email, order_url=None):
    customer_email = normalize_email(customer_email)

    order_code = _safe_str(_order_value(order, "order_code", ""))
    order_id = _order_value(order, "id", None)

    subject = f"FUMAP GO｜訂單已送達｜{order_code or 'UNKNOWN'}"

    store_name = _safe_str(
        _order_value(order, "store_name", "")
        or _order_value(order, "manual_order_title", "")
        or _order_value(order, "store_title", "")
    )
    delivered_at = _safe_str(
        _order_value(order, "delivery_proof_uploaded_at", "")
        or _order_value(order, "updated_at", "")
    )
    status = _safe_str(_order_value(order, "status", "DELIVERED"))
    payment_method = _safe_str(_order_value(order, "payment_method", ""))
    payment_status = _safe_str(_order_value(order, "payment_status", ""))

    order_url = absolute_url(order_url or f"/orders?order_code={order_code}")

    info_rows = [
        ("訂單編號", order_code or "UNKNOWN"),
        ("店家名稱", store_name or "-"),
        ("配送狀態", status or "DELIVERED"),
        ("付款方式", payment_method or "-"),
        ("付款狀態", payment_status or "-"),
    ]

    if delivered_at:
        info_rows.append(("送達時間", delivered_at))

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
        button_url=order_url,
        info_rows=info_rows,
    )

    text_body = f"""您好，

您的 FUMAP GO 訂單已送達。

訂單編號：{order_code or 'UNKNOWN'}
店家名稱：{store_name or '-'}
配送狀態：{status or 'DELIVERED'}
付款方式：{payment_method or '-'}
付款狀態：{payment_status or '-'}
送達時間：{delivered_at or '-'}

查看訂單狀態：
{order_url}

若有配送證明，系統會保存於訂單紀錄中。
為了保護隱私，V1 不會將圖片作為附件寄出。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=customer_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="ORDER_DELIVERED_EMAIL",
        recipient_role="CUSTOMER",
        order_id=order_id,
        order_code=order_code,
    )

def _settlement_value(settlement, key, default=""):
    if settlement is None:
        return default

    try:
        if key in settlement.keys():
            value = settlement[key]
            return default if value is None else value
    except Exception:
        pass

    try:
        value = settlement.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def _settlement_url():
    return absolute_url("/admin/settlements")


def _target_email(target):
    return normalize_email(
        _row_get(target, "email", "")
        or _row_get(target, "target_email", "")
    )


def _target_user_id(target):
    return _row_get(target, "user_id", None) or _row_get(target, "target_user_id", None)


def _target_display_name(target, role):
    role = _safe_str(role).upper()

    if role == "STORE":
        return (
            _safe_str(_row_get(target, "store_name", ""))
            or _safe_str(_row_get(target, "display_name", ""))
            or _safe_str(_row_get(target, "target_code", ""))
            or "店家"
        )

    if role == "DRIVER":
        return (
            _safe_str(_row_get(target, "driver_name", ""))
            or _safe_str(_row_get(target, "display_name", ""))
            or _safe_str(_row_get(target, "target_code", ""))
            or "Shiper"
        )

    return _safe_str(_row_get(target, "display_name", "")) or "FUMAP GO 用戶"


def _admin_payment_info_rows(admin_payment_info):
    admin_payment_info = admin_payment_info or {}

    return [
        ("Admin 收款銀行", admin_payment_info.get("bank_name") or "-"),
        ("銀行代碼", admin_payment_info.get("bank_code") or "-"),
        ("銀行帳號", admin_payment_info.get("bank_account") or "-"),
        ("轉帳備註", admin_payment_info.get("bank_note") or "-"),
        ("LINE Pay 名稱", admin_payment_info.get("linepay_name") or "-"),
    ]


def _payout_info_rows(payout_info):
    payout_info = payout_info or {}

    return [
        ("收款戶名", payout_info.get("payout_account_name") or "-"),
        ("銀行名稱", payout_info.get("payout_bank_name") or "-"),
        ("銀行代碼", payout_info.get("payout_bank_code") or "-"),
        ("銀行帳號", payout_info.get("payout_bank_account") or "-"),
        ("備註", payout_info.get("payout_note") or "-"),
    ]


def _settlement_basic_rows(settlement):
    settlement_code = _safe_str(_settlement_value(settlement, "settlement_code", ""))
    period_start = _safe_str(_settlement_value(settlement, "period_start", ""))
    period_end = _safe_str(_settlement_value(settlement, "period_end", ""))
    amount_twd = _format_twd(_settlement_value(settlement, "amount_twd", 0))
    status = _safe_str(_settlement_value(settlement, "status", ""))

    return [
        ("結算單號", settlement_code or "-"),
        ("結算期間", f"{period_start or '-'} ~ {period_end or '-'}"),
        ("結算金額", amount_twd),
        ("狀態", status or "-"),
    ]


def send_store_payment_request_email(store, settlement, admin_payment_info):
    to_email = _target_email(store)
    store_name = _target_display_name(store, "STORE")
    settlement_code = _safe_str(_settlement_value(settlement, "settlement_code", ""))
    amount_twd = _format_twd(_settlement_value(settlement, "amount_twd", 0))
    user_id = _target_user_id(store)

    subject = f"FUMAP GO｜店家平台費結算通知｜{settlement_code or 'SETTLEMENT'}"

    body_html = f"""
    <p style="margin:0 0 12px 0;">{html.escape(store_name)} 您好，</p>
    <p style="margin:0 0 12px 0;">
      這是 FUMAP GO 店家平台費結算通知。
    </p>
    <p style="margin:0 0 12px 0;">
      本次店家需支付 Admin 的平台費為：
      <b>{html.escape(amount_twd)}</b>
    </p>
    <p style="margin:0 0 12px 0;">
      請依下方 Admin 收款帳戶完成轉帳。完成後請通知 Admin，由 Admin 在系統確認收款。
    </p>
    <p style="margin:0;color:#6B7280;">
      此為內部結算通知，不會自動扣款，也不代表銀行已完成交易。
    </p>
    """

    info_rows = []
    info_rows.extend(_settlement_basic_rows(settlement))
    info_rows.extend(_admin_payment_info_rows(admin_payment_info))

    html_body = render_branded_email(
        title="店家平台費結算通知",
        body_html=body_html,
        button_text="查看 Admin 結算頁",
        button_url=_settlement_url(),
        info_rows=info_rows,
    )

    text_body = f"""您好，

這是 FUMAP GO 店家平台費結算通知。

店家：{store_name}
結算單號：{settlement_code or '-'}
結算金額：{amount_twd}

請依下方 Admin 收款帳戶完成轉帳。完成後請通知 Admin，由 Admin 在系統確認收款。

Admin 收款帳戶：
銀行名稱：{admin_payment_info.get('bank_name') or '-'}
銀行代碼：{admin_payment_info.get('bank_code') or '-'}
銀行帳號：{admin_payment_info.get('bank_account') or '-'}
轉帳備註：{admin_payment_info.get('bank_note') or '-'}

此為內部結算通知，不會自動扣款，也不代表銀行已完成交易。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="STORE_SETTLEMENT_PAYMENT_REQUEST",
        recipient_role="STORE",
        user_id=user_id,
    )


def send_driver_payment_request_email(driver, settlement, admin_payment_info):
    to_email = _target_email(driver)
    driver_name = _target_display_name(driver, "DRIVER")
    settlement_code = _safe_str(_settlement_value(settlement, "settlement_code", ""))
    amount_twd = _format_twd(_settlement_value(settlement, "amount_twd", 0))
    user_id = _target_user_id(driver)

    subject = f"FUMAP GO｜Shiper 平台費結算通知｜{settlement_code or 'SETTLEMENT'}"

    body_html = f"""
    <p style="margin:0 0 12px 0;">{html.escape(driver_name)} 您好，</p>
    <p style="margin:0 0 12px 0;">
      這是 FUMAP GO Shiper 平台費結算通知。
    </p>
    <p style="margin:0 0 12px 0;">
      本次 Shiper 需支付 Admin 的平台費為：
      <b>{html.escape(amount_twd)}</b>
    </p>
    <p style="margin:0 0 12px 0;">
      請依下方 Admin 收款帳戶完成轉帳。完成後請通知 Admin，由 Admin 在系統確認收款。
    </p>
    <p style="margin:0;color:#6B7280;">
      Shiper 只結算自己的平台費，不代收店家平台費。
    </p>
    """

    info_rows = []
    info_rows.extend(_settlement_basic_rows(settlement))
    info_rows.extend(_admin_payment_info_rows(admin_payment_info))

    html_body = render_branded_email(
        title="Shiper 平台費結算通知",
        body_html=body_html,
        button_text="查看 Admin 結算頁",
        button_url=_settlement_url(),
        info_rows=info_rows,
    )

    text_body = f"""您好，

這是 FUMAP GO Shiper 平台費結算通知。

Shiper：{driver_name}
結算單號：{settlement_code or '-'}
結算金額：{amount_twd}

請依下方 Admin 收款帳戶完成轉帳。完成後請通知 Admin，由 Admin 在系統確認收款。

Admin 收款帳戶：
銀行名稱：{admin_payment_info.get('bank_name') or '-'}
銀行代碼：{admin_payment_info.get('bank_code') or '-'}
銀行帳號：{admin_payment_info.get('bank_account') or '-'}
轉帳備註：{admin_payment_info.get('bank_note') or '-'}

Shiper 只結算自己的平台費，不代收店家平台費。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="DRIVER_SETTLEMENT_PAYMENT_REQUEST",
        recipient_role="DRIVER",
        user_id=user_id,
    )


def send_store_payout_confirmed_email(store, settlement):
    to_email = _target_email(store)
    store_name = _target_display_name(store, "STORE")
    settlement_code = _safe_str(_settlement_value(settlement, "settlement_code", ""))
    amount_twd = _format_twd(_settlement_value(settlement, "amount_twd", 0))
    confirmed_at = _safe_str(_settlement_value(settlement, "paid_confirmed_at", ""))
    user_id = _target_user_id(store)

    payout_info = _settlement_value(settlement, "target_payout_snapshot", None) or {}

    if isinstance(payout_info, str):
        payout_info = {}

    subject = f"FUMAP GO｜Admin 已完成店家撥款｜{settlement_code or 'SETTLEMENT'}"

    body_html = f"""
    <p style="margin:0 0 12px 0;">{html.escape(store_name)} 您好，</p>
    <p style="margin:0 0 12px 0;">
      Admin 已在系統確認本次店家撥款完成。
    </p>
    <p style="margin:0 0 12px 0;">
      本次撥款金額為：<b>{html.escape(amount_twd)}</b>
    </p>
    <p style="margin:0;color:#6B7280;">
      請確認您的收款帳戶是否已收到款項。如有疑問，請聯絡 Admin。
    </p>
    """

    info_rows = []
    info_rows.extend(_settlement_basic_rows(settlement))

    if confirmed_at:
        info_rows.append(("確認時間", confirmed_at))

    info_rows.extend(_payout_info_rows(payout_info))

    html_body = render_branded_email(
        title="Admin 已完成店家撥款",
        body_html=body_html,
        button_text="查看店家對帳",
        button_url="/store/accounting",
        info_rows=info_rows,
    )

    text_body = f"""您好，

Admin 已在系統確認本次店家撥款完成。

店家：{store_name}
結算單號：{settlement_code or '-'}
撥款金額：{amount_twd}
確認時間：{confirmed_at or '-'}

請確認您的收款帳戶是否已收到款項。如有疑問，請聯絡 Admin。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="STORE_PAYOUT_CONFIRMED",
        recipient_role="STORE",
        user_id=user_id,
    )


def send_driver_payout_confirmed_email(driver, settlement):
    to_email = _target_email(driver)
    driver_name = _target_display_name(driver, "DRIVER")
    settlement_code = _safe_str(_settlement_value(settlement, "settlement_code", ""))
    amount_twd = _format_twd(_settlement_value(settlement, "amount_twd", 0))
    confirmed_at = _safe_str(_settlement_value(settlement, "paid_confirmed_at", ""))
    user_id = _target_user_id(driver)

    payout_info = _settlement_value(settlement, "target_payout_snapshot", None) or {}

    if isinstance(payout_info, str):
        payout_info = {}

    subject = f"FUMAP GO｜Admin 已完成 Shiper 撥款｜{settlement_code or 'SETTLEMENT'}"

    body_html = f"""
    <p style="margin:0 0 12px 0;">{html.escape(driver_name)} 您好，</p>
    <p style="margin:0 0 12px 0;">
      Admin 已在系統確認本次 Shiper 撥款完成。
    </p>
    <p style="margin:0 0 12px 0;">
      本次撥款金額為：<b>{html.escape(amount_twd)}</b>
    </p>
    <p style="margin:0;color:#6B7280;">
      請確認您的收款帳戶是否已收到款項。如有疑問，請聯絡 Admin。
    </p>
    """

    info_rows = []
    info_rows.extend(_settlement_basic_rows(settlement))

    if confirmed_at:
        info_rows.append(("確認時間", confirmed_at))

    info_rows.extend(_payout_info_rows(payout_info))

    html_body = render_branded_email(
        title="Admin 已完成 Shiper 撥款",
        body_html=body_html,
        button_text="查看 Shiper 對帳",
        button_url="/driver/accounting",
        info_rows=info_rows,
    )

    text_body = f"""您好，

Admin 已在系統確認本次 Shiper 撥款完成。

Shiper：{driver_name}
結算單號：{settlement_code or '-'}
撥款金額：{amount_twd}
確認時間：{confirmed_at or '-'}

請確認您的收款帳戶是否已收到款項。如有疑問，請聯絡 Admin。

FUMAP GO
fumapgo.com
"""

    return send_email(
        to_email=to_email,
        subject=subject,
        html_body=html_body,
        text_body=text_body,
        event_type="DRIVER_PAYOUT_CONFIRMED",
        recipient_role="DRIVER",
        user_id=user_id,
    )

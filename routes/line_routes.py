from flask import Blueprint, render_template, request, redirect, flash, jsonify, session, current_app

from db import get_db
from services.permission_service import login_required, current_user, role_home
from services.line_bind_service import (
    LineBindError,
    bind_line_contact,
    bind_line_user_to_current_account,
    current_user_line_context,
    disable_binding,
    get_active_binding_by_role_target,
    list_bindings,
)
from services.line_notify_service import (
    push_admin_direct,
    push_to_role_target,
    push_user_bind_success,
)
from services.email_service import (
    send_admin_line_bind_success_email,
    send_line_bind_success_email,
)


line_bp = Blueprint("line", __name__, url_prefix="/line")


def _safe_text(value, default=""):
    value = "" if value is None else str(value)
    value = value.strip()
    return value if value else default


def _result_ok(result):
    """
    Normalize notification result.

    LINE helper usually returns:
    {"ok": True/False, "skipped": True/False, ...}

    Email helper returns True/False.
    """
    if isinstance(result, bool):
        return result

    if isinstance(result, dict):
        return bool(result.get("ok")) and not bool(result.get("skipped"))

    return bool(result)


def _user_email(user):
    try:
        return (user["email"] or "").strip().lower()
    except Exception:
        return ""


def _user_phone(user):
    try:
        return (user["phone"] or "").strip()
    except Exception:
        return ""


def _user_display_name(user):
    try:
        return (user["display_name"] or "").strip() or (user["login_id"] or "").strip()
    except Exception:
        return ""


def _line_admin_url():
    return (
        current_app.config.get("LINE_ADMIN_URL")
        or ""
    ).strip()


def _liff_id():
    return (
        current_app.config.get("LINE_LIFF_ID")
        or ""
    ).strip()


def _notify_admin_line_bind_success(db, *, user, ctx, binding):
    """
    Notify Admin by LINE direct after One-Tap LINE bind.

    Uses FGO_ADMIN_LINE_USER_ID through push_admin_direct().
    Failure must not rollback bind.
    """
    try:
        role = ctx.get("role", "")
        role_label = ctx.get("role_label", role)
        target_code = ctx.get("target_code", "")
        line_display_name = ""

        try:
            line_display_name = binding["line_display_name"] or ""
        except Exception:
            line_display_name = ""

        message = (
            "FUMAP GO 有新的 LINE 綁定成功\n"
            f"角色：{role_label} / {role}\n"
            f"代碼：{target_code}\n"
            f"LINE 名稱：{line_display_name or '-'}\n"
            f"帳號名稱：{_user_display_name(user) or '-'}\n"
            f"Email：{_user_email(user) or '-'}\n"
            f"電話：{_user_phone(user) or '-'}\n"
            "之後此帳號可接收 LINE 通知。"
        )

        return push_admin_direct(
            db,
            event_type="LINE_BIND_SUCCESS_ADMIN_NOTIFY",
            message=message,
            order_code="",
            commit=True,
        )

    except Exception as exc:
        print(f"[LINE_BIND][ADMIN_LINE_NOTIFY][ERROR] {exc}")
        return {"ok": False, "error": str(exc)}


def _notify_admin_line_bind_success_by_email(*, user, ctx, binding):
    """
    Notify Admin by Email after One-Tap LINE bind.

    Failure must not rollback bind.
    """
    try:
        return send_admin_line_bind_success_email(
            user=user,
            role=ctx.get("role", ""),
            role_label=ctx.get("role_label", ""),
            target_code=ctx.get("target_code", ""),
            line_display_name=binding["line_display_name"] if binding else "",
            contact_code=binding["contact_code"] if binding else "",
        )

    except Exception as exc:
        print(f"[LINE_BIND][ADMIN_EMAIL_NOTIFY][ERROR] {exc}")
        return False


def _notify_user_line_bind_success(db, *, user, ctx, binding):
    """
    Notify user by LINE + Email after successful bind.

    Failure must not rollback bind.
    """
    role = ctx.get("role", "")
    role_label = ctx.get("role_label", role)
    target_code = ctx.get("target_code", "")

    line_result = {"ok": False, "skipped": True}
    email_result = False

    try:
        line_result = push_user_bind_success(
            db,
            role=role,
            target_code=target_code,
            line_display_name=binding["line_display_name"] if binding else "",
            commit=True,
        )
    except Exception as exc:
        print(f"[LINE_BIND][USER_LINE_NOTIFY][ERROR] {exc}")
        line_result = {"ok": False, "error": str(exc)}

    try:
        email = _user_email(user)

        if email:
            email_result = send_line_bind_success_email(
                email,
                role=role,
                role_label=role_label,
                target_code=target_code,
                line_display_name=binding["line_display_name"] if binding else "",
                user_id=user["id"],
            )
    except Exception as exc:
        print(f"[LINE_BIND][USER_EMAIL_NOTIFY][ERROR] {exc}")
        email_result = False

    return {
        "user_line_result": line_result,
        "user_email_result": email_result,
        "user_line_notified": _result_ok(line_result),
        "user_email_notified": _result_ok(email_result),
    }


@line_bp.get("/bind")
@login_required
def bind_page():
    db = get_db()
    user = current_user()

    if not user:
        flash("請先登入。", "warning")
        return redirect("/login")

    try:
        ctx = current_user_line_context(db, user)
        error = ""
    except Exception as exc:
        ctx = {
            "role": user["role"] if user else "",
            "role_label": user["role"] if user else "",
            "target_code": "",
            "binding": None,
            "active": False,
            "contact_code": "",
            "line_display_name": "",
        }
        error = str(exc)

    return render_template(
        "mobile/line/bind.html",
        ctx=ctx,
        error=error,
        liff_id=_liff_id(),
        line_admin_url=_line_admin_url(),
    )


@line_bp.post("/bind")
@login_required
def bind_submit():
    """
    Backward-compatible manual bind/unbind route.

    The new preferred flow is POST /line/bind/one-tap from LIFF.
    """
    db = get_db()
    user = current_user()

    if not user:
        flash("請先登入。", "warning")
        return redirect("/login")

    action = request.form.get("action", "bind")

    try:
        if action == "unbind":
            disable_binding(db, user=user, commit=True)
            flash("LINE 綁定已停用。", "success")
            return redirect("/line/bind")

        binding = bind_line_contact(
            db,
            user=user,
            line_user_id=request.form.get("line_user_id", ""),
            line_display_name=request.form.get("line_display_name", ""),
            commit=True,
        )

        flash(
            f"LINE 綁定完成。你的通知代碼是 {binding['contact_code']}。LINE 只用於通知，不用於登入或授權。",
            "success",
        )

        return redirect("/line/bind")

    except LineBindError as exc:
        flash(str(exc), "danger")
        return redirect("/line/bind")

    except Exception as exc:
        db.rollback()
        flash(f"LINE 綁定失敗：{exc}", "danger")
        return redirect("/line/bind")


@line_bp.post("/bind/one-tap")
@login_required
def bind_one_tap():
    """
    One-Tap LIFF bind.

    Frontend sends:
    {
      line_user_id: profile.userId,
      line_display_name: profile.displayName,
      picture_url: profile.pictureUrl
    }

    Backend:
    - validates logged-in webapp user
    - binds LINE userId to current role/target_code
    - notifies Admin by LINE + Email
    - notifies user by LINE + Email
    """
    db = get_db()
    user = current_user()
    payload = request.get_json(silent=True) or {}

    if not user:
        return jsonify({"ok": False, "error": "login required"}), 401

    line_user_id = _safe_text(
        payload.get("line_user_id")
        or payload.get("userId")
        or payload.get("user_id")
        or ""
    )
    line_display_name = _safe_text(
        payload.get("line_display_name")
        or payload.get("displayName")
        or payload.get("display_name")
        or ""
    )
    picture_url = _safe_text(payload.get("picture_url") or payload.get("pictureUrl") or "")

    if not line_user_id:
        return jsonify({"ok": False, "error": "missing LINE userId"}), 400

    if not line_user_id.startswith("U"):
        return jsonify({"ok": False, "error": "invalid LINE userId"}), 400

    try:
        binding = bind_line_user_to_current_account(
            db,
            user=user,
            line_user_id=line_user_id,
            line_display_name=line_display_name,
            picture_url=picture_url,
            commit=True,
        )

        ctx = current_user_line_context(db, user)

        # Notification must not rollback bind.
        admin_line_result = _notify_admin_line_bind_success(
            db,
            user=user,
            ctx=ctx,
            binding=binding,
        )

        admin_email_result = _notify_admin_line_bind_success_by_email(
            user=user,
            ctx=ctx,
            binding=binding,
        )

        user_notify = _notify_user_line_bind_success(
            db,
            user=user,
            ctx=ctx,
            binding=binding,
        )

        return jsonify(
            {
                "ok": True,
                "role": ctx["role"],
                "role_label": ctx["role_label"],
                "target_code": ctx["target_code"],
                "active": ctx["active"],
                "contact_code": binding["contact_code"],
                "line_display_name": binding["line_display_name"],
                "user_line_notified": bool(user_notify.get("user_line_notified")),
                "user_email_notified": bool(user_notify.get("user_email_notified")),
                "admin_line_notified": _result_ok(admin_line_result),
                "admin_email_notified": _result_ok(admin_email_result),
                "notification": {
                    "user_line": user_notify.get("user_line_result"),
                    "user_email": bool(user_notify.get("user_email_result")),
                    "admin_line": admin_line_result,
                    "admin_email": bool(admin_email_result),
                },
                "note": "LINE bound for notification only. No login, no account creation, no permission grant.",
            }
        )

    except LineBindError as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400

    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": f"LINE 綁定失敗：{exc}"}), 500


@line_bp.post("/bind/request")
@login_required
def bind_request_admin_help():
    """
    Fallback when LIFF fails.

    This does not bind LINE userId.
    It only pushes Admin direct so Admin can help the user.
    """
    db = get_db()
    user = current_user()

    if not user:
        return jsonify({"ok": False, "error": "login required"}), 401

    try:
        ctx = current_user_line_context(db, user)

        message = (
            "FUMAP GO LINE 綁定協助請求\n"
            f"角色：{ctx.get('role_label') or ctx.get('role')}\n"
            f"代碼：{ctx.get('target_code') or '-'}\n"
            f"帳號名稱：{_user_display_name(user) or '-'}\n"
            f"Email：{_user_email(user) or '-'}\n"
            f"電話：{_user_phone(user) or '-'}\n"
            "使用者表示 LIFF 綁定失敗，需要 Admin 協助。"
        )

        result = push_admin_direct(
            db,
            event_type="LINE_BIND_HELP_REQUESTED",
            message=message,
            order_code="",
            commit=True,
        )

        return jsonify(
            {
                "ok": True,
                "result": result,
                "message": "已通知 Admin 協助綁定。",
            }
        )

    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400


@line_bp.get("/status")
@login_required
def status_page():
    return redirect("/line/bind")


@line_bp.get("/api/status")
@login_required
def api_status():
    db = get_db()
    user = current_user()

    if not user:
        return jsonify({"ok": False, "error": "login required"}), 401

    try:
        ctx = current_user_line_context(db, user)

        return jsonify(
            {
                "ok": True,
                "role": ctx["role"],
                "role_label": ctx["role_label"],
                "target_code": ctx["target_code"],
                "active": ctx["active"],
                "contact_code": ctx["contact_code"],
                "line_display_name": ctx["line_display_name"],
            }
        )

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@line_bp.get("/contacts")
@login_required
def contacts_page():
    user = current_user()

    if not user or user["role"] != "ADMIN_OPERATOR":
        flash("只有 Admin 可以查看 LINE 綁定清單。", "danger")
        return redirect(role_home(user["role"] if user else "CUSTOMER"))

    db = get_db()
    rows = list_bindings(db, limit=200)

    return render_template(
        "mobile/line/contacts.html",
        bindings=rows,
    )


@line_bp.get("/contact/<role>/<target_code>")
@login_required
def contact_detail(role, target_code):
    user = current_user()

    if not user or user["role"] != "ADMIN_OPERATOR":
        flash("只有 Admin 可以查看 LINE 綁定資料。", "danger")
        return redirect(role_home(user["role"] if user else "CUSTOMER"))

    db = get_db()
    binding = get_active_binding_by_role_target(db, role, target_code)

    if not binding:
        flash("找不到有效 LINE 綁定。", "warning")
        return redirect("/line/contacts")

    return render_template(
        "mobile/line/contact_detail.html",
        binding=binding,
    )


@line_bp.post("/contact/<role>/<target_code>/test")
@login_required
def contact_test(role, target_code):
    user = current_user()

    if not user or user["role"] != "ADMIN_OPERATOR":
        return jsonify({"ok": False, "error": "admin only"}), 403

    db = get_db()

    try:
        result = push_to_role_target(
            db,
            role=role,
            target_code=target_code,
            event_type="LINE_TEST",
            order_code="",
            message=(
                "FUMAP GO 測試通知\n"
                f"角色：{role}\n"
                f"代碼：{target_code}\n"
                "如果您收到此訊息，代表 LINE 通知已可正常接收。"
            ),
            commit=True,
        )

        return jsonify({"ok": bool(result.get("ok")), "result": result})

    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400

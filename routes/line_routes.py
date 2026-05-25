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
from services.email_service import send_line_bind_success_email


line_bp = Blueprint("line", __name__, url_prefix="/line")


def _safe_text(value, default=""):
    value = "" if value is None else str(value)
    value = value.strip()
    return value if value else default


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
    Notify Admin directly after One-Tap LINE bind.

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
        print(f"[LINE_BIND][ADMIN_NOTIFY][ERROR] {exc}")
        return {"ok": False, "error": str(exc)}


def _notify_user_line_bind_success(db, *, user, ctx, binding):
    """
    Notify user by LINE + Email after successful bind.

    Failure must not rollback bind.
    """
    role = ctx.get("role", "")
    role_label = ctx.get("role_label", role)
    target_code = ctx.get("target_code", "")

    try:
        push_user_bind_success(
            db,
            role=role,
            target_code=target_code,
            line_display_name=binding["line_display_name"] if binding else "",
            commit=True,
        )
    except Exception as exc:
        print(f"[LINE_BIND][USER_LINE_NOTIFY][ERROR] {exc}")

    try:
        email = _user_email(user)

        if email:
            send_line_bind_success_email(
                email,
                role=role,
                role_label=role_label,
                target_code=target_code,
                line_display_name=binding["line_display_name"] if binding else "",
                user_id=user["id"],
            )
    except Exception as exc:
        print(f"[LINE_BIND][USER_EMAIL_NOTIFY][ERROR] {exc}")


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
    - notifies Admin direct
    - notifies user by LINE and Email
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
        _notify_admin_line_bind_success(
            db,
            user=user,
            ctx=ctx,
            binding=binding,
        )

        _notify_user_line_bind_success(
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

    try:
        ctx = current_user_line_context(db, user)
        binding = ctx.get("binding")

        return jsonify(
            {
                "ok": True,
                "role": ctx["role"],
                "role_label": ctx["role_label"],
                "target_code": ctx["target_code"],
                "active": ctx["active"],
                "contact_code": ctx["contact_code"],
                "binding": dict(binding) if binding else None,
                "note": "LINE is notification contact only. It does not grant permission.",
            }
        )

    except Exception as exc:
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
            }
        ), 400


@line_bp.post("/api/bind")
@login_required
def api_bind():
    """
    Backward-compatible API bind route.
    Prefer /line/bind/one-tap for LIFF.
    """
    db = get_db()
    user = current_user()
    payload = request.get_json(silent=True) or {}

    if not user:
        return jsonify({"ok": False, "error": "login required"}), 401

    try:
        binding = bind_line_contact(
            db,
            user=user,
            line_user_id=payload.get("line_user_id") or payload.get("userId") or "",
            line_display_name=payload.get("line_display_name") or payload.get("displayName") or "",
            commit=True,
        )

        return jsonify(
            {
                "ok": True,
                "binding": dict(binding),
                "contact_code": binding["contact_code"],
                "note": "LINE bound for notification only. No login, no account creation, no permission grant.",
            }
        )

    except Exception as exc:
        db.rollback()
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
            }
        ), 400


@line_bp.get("/api/check")
def api_check():
    """
    Public-safe check for internal debugging.

    This does not grant login or permission.
    """
    db = get_db()
    role = (request.args.get("role") or "").strip().upper()
    target_code = (request.args.get("target_code") or "").strip().upper()

    if not role or not target_code:
        return jsonify(
            {
                "ok": False,
                "error": "role and target_code required",
            }
        ), 400

    binding = get_active_binding_by_role_target(db, role, target_code)

    return jsonify(
        {
            "ok": True,
            "active": bool(binding),
            "binding": dict(binding) if binding else None,
            "note": "This endpoint only checks LINE contact binding. It does not authenticate users.",
        }
    )


@line_bp.post("/api/test-push")
@login_required
def api_test_push():
    db = get_db()
    user = current_user()

    if not user:
        return jsonify({"ok": False, "error": "login required"}), 401

    try:
        ctx = current_user_line_context(db, user)

        if not ctx["active"]:
            return jsonify(
                {
                    "ok": False,
                    "error": "LINE binding inactive",
                }
            ), 400

        result = push_to_role_target(
            db,
            role=ctx["role"],
            target_code=ctx["target_code"],
            event_type="TEST_PUSH",
            message=(
                "FUMAP GO 測試通知\n\n"
                f"角色：{ctx['role_label']}\n"
                f"通知代碼：{ctx['contact_code']}\n\n"
                "LINE 只用於通知，不用於登入或授權。"
            ),
            commit=True,
        )

        return jsonify(
            {
                "ok": bool(result.get("ok")),
                "result": result,
            }
        )

    except Exception as exc:
        db.rollback()
        return jsonify(
            {
                "ok": False,
                "error": str(exc),
            }
        ), 400


@line_bp.post("/api/test-admin-push")
@login_required
def api_test_admin_push():
    """
    Test FGO_ADMIN_LINE_USER_ID direct push.
    Only Admin operator should use this.
    """
    if session.get("role") != "ADMIN_OPERATOR":
        return jsonify({"ok": False, "error": "admin only"}), 403

    db = get_db()

    try:
        result = push_admin_direct(
            db,
            event_type="TEST_ADMIN_DIRECT_PUSH",
            message=(
                "FUMAP GO Admin Direct Push 測試\n"
                "如果您收到此訊息，代表 FGO_ADMIN_LINE_USER_ID 與 LINE Gateway 正常。"
            ),
            order_code="",
            commit=True,
        )

        return jsonify(
            {
                "ok": bool(result.get("ok")),
                "result": result,
            }
        )

    except Exception as exc:
        db.rollback()
        return jsonify({"ok": False, "error": str(exc)}), 400


@line_bp.get("/debug")
def debug_list():
    """
    Lightweight debug page.
    Only available if logged in as admin operator.
    """
    if session.get("role") != "ADMIN_OPERATOR":
        flash("此頁面僅限 Admin。", "danger")
        return redirect(role_home(session.get("role")))

    rows = list_bindings(get_db(), limit=200)

    return jsonify(
        {
            "ok": True,
            "bindings": [dict(row) for row in rows],
        }
    )

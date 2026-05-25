from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    flash,
    session,
    current_app,
    url_for,
)

from db import get_db
from services.auth_service import (
    AuthError,
    REGISTER_ROLES,
    authenticate_admin,
    authenticate_user,
    can_resend_email_verification,
    change_user_password,
    create_email_verification_token,
    create_password_reset_token,
    get_role_target,
    get_user_by_valid_reset_token,
    normalize_email,
    register_user,
    reset_password_with_token,
    session_payload,
    verify_email_with_token,
)
from services.permission_service import login_required, current_user, role_home
from services.block_service import create_auth_block
from services.email_service import (
    send_email_verification,
    send_password_reset_email,
)


auth_bp = Blueprint("auth", __name__)


def _safe_next(default="/"):
    next_url = request.args.get("next") or request.form.get("next") or default

    if not next_url.startswith("/"):
        return default

    if next_url.startswith("//"):
        return default

    return next_url


def _base_url():
    configured = (
        current_app.config.get("APP_BASE_URL")
        or current_app.config.get("PUBLIC_BASE_URL")
        or ""
    ).strip().rstrip("/")

    if configured:
        return configured

    return request.url_root.rstrip("/")


def _reset_url(raw_token):
    return f"{_base_url()}{url_for('auth.reset_password_page')}?token={raw_token}"


def _verify_email_url(raw_token):
    return f"{_base_url()}{url_for('auth.verify_email_page')}?token={raw_token}"


def _send_verification_email_for_user(db, user):
    if not user:
        return False

    email = normalize_email(user["email"] if "email" in user.keys() else "")

    if not email:
        return False

    token_data = create_email_verification_token(db, user["id"])

    if not token_data:
        return False

    verify_url = _verify_email_url(token_data["raw_token"])

    return send_email_verification(
        token_data["user"],
        verify_url,
        user_id=user["id"],
    )


@auth_bp.get("/register")
def register_page():
    role = (request.args.get("role") or "CUSTOMER").upper()

    if role not in REGISTER_ROLES:
        role = "CUSTOMER"

    return render_template(
        "mobile/auth/register.html",
        selected_role=role,
        roles=sorted(REGISTER_ROLES),
    )


@auth_bp.post("/register")
def register_submit():
    db = get_db()

    login_id = request.form.get("login_id", "")
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")
    display_name = request.form.get("display_name", "")
    phone = request.form.get("phone", "")
    email = request.form.get("email", "")
    role = request.form.get("role", "CUSTOMER")

    if password != confirm_password:
        flash("兩次密碼不一致。", "danger")
        return redirect(f"/register?role={role}")

    try:
        user = register_user(
            db,
            login_id=login_id,
            password=password,
            display_name=display_name,
            phone=phone,
            email=email,
            role=role,
        )

        create_auth_block(
            db,
            event_type="USER_REGISTERED",
            user=user,
            payload={
                "login_id": user["login_id"],
                "role": user["role"],
                "display_name": user["display_name"],
                "email_saved": bool(user["email"] if "email" in user.keys() else ""),
            },
            commit=True,
        )

        email_saved = bool(user["email"] if "email" in user.keys() else "")

        if email_saved:
            try:
                _send_verification_email_for_user(db, user)
            except Exception as email_exc:
                print(f"[AUTH][VERIFY_EMAIL][ERROR] {email_exc}")

            flash("註冊完成，確認信已寄出，請到 Email 點擊確認連結。", "success")
        else:
            flash("註冊完成，請登入。", "success")

        return redirect("/login")

    except AuthError as e:
        flash(str(e), "danger")
        return redirect(f"/register?role={role}")

    except Exception as e:
        db.rollback()
        flash(f"註冊失敗：{e}", "danger")
        return redirect(f"/register?role={role}")


@auth_bp.get("/verify-email")
def verify_email_page():
    db = get_db()
    token = request.args.get("token", "").strip()

    try:
        user = verify_email_with_token(db, token)

        create_auth_block(
            db,
            event_type="USER_EMAIL_VERIFIED",
            user=user,
            payload={
                "login_id": user["login_id"],
                "role": user["role"],
                "email_verified": True,
            },
            commit=True,
        )

        flash("Email 已驗證。", "success")

        logged_in_user_id = session.get("user_id")

        if logged_in_user_id and logged_in_user_id == user["id"]:
            return redirect(role_home(user["role"]))

        return redirect("/login")

    except AuthError as exc:
        flash(str(exc), "danger")
        return redirect("/login")

    except Exception as exc:
        db.rollback()
        print(f"[AUTH][VERIFY_EMAIL][ERROR] {exc}")
        flash("驗證連結已失效，請重新申請。", "danger")
        return redirect("/login")


@auth_bp.post("/account/email/resend")
@login_required
def resend_email_verification():
    db = get_db()
    user = current_user()

    if not user:
        flash("請重新登入。", "warning")
        return redirect("/login")

    if user["role"] == "ADMIN_OPERATOR":
        flash("Admin 設定帳號目前不支援 Email 驗證。", "warning")
        return redirect("/admin")

    ok, message = can_resend_email_verification(user)

    if not ok:
        flash(message, "warning")
        return redirect(request.referrer or role_home(user["role"]))

    try:
        sent = _send_verification_email_for_user(db, user)

        if sent:
            flash("確認信已寄出，請到 Email 點擊確認連結。", "success")
        else:
            flash("確認信已建立，但目前 Email 服務尚未完成設定。", "warning")

        return redirect(request.referrer or role_home(user["role"]))

    except Exception as exc:
        db.rollback()
        print(f"[AUTH][RESEND_VERIFY_EMAIL][ERROR] {exc}")
        flash("確認信寄送失敗，請稍後再試。", "danger")
        return redirect(request.referrer or role_home(user["role"]))


@auth_bp.get("/login")
def login_page():
    return render_template(
        "mobile/auth/login.html",
        next_url=_safe_next("/"),
    )


@auth_bp.post("/login")
def login_submit():
    db = get_db()

    login_id = request.form.get("login_id", "")
    password = request.form.get("password", "")
    remember_me = request.form.get("remember_me") == "1"
    next_url = _safe_next("/")

    admin = authenticate_admin(current_app.config, login_id, password)

    if admin:
        session.clear()
        session.permanent = bool(remember_me)
        session["user_id"] = 0
        session["login_id"] = admin["login_id"]
        session["role"] = "ADMIN_OPERATOR"
        session["display_name"] = admin["display_name"]
        session["phone"] = ""
        session["target_code"] = ""

        flash("Admin 已登入。", "success")
        return redirect(next_url if next_url != "/" else "/admin")

    try:
        user = authenticate_user(db, login_id, password)
        target = get_role_target(db, user)
        payload = session_payload(user, target)

        session.clear()
        session.permanent = bool(remember_me)

        for key, value in payload.items():
            session[key] = value

        create_auth_block(
            db,
            event_type="USER_LOGIN",
            user=user,
            payload={
                "login_id": user["login_id"],
                "role": user["role"],
                "target_code": payload.get("target_code", ""),
                "remember_me": remember_me,
            },
            commit=True,
        )

        flash("登入成功。", "success")
        return redirect(next_url if next_url != "/" else role_home(user["role"]))

    except AuthError as e:
        flash(str(e), "danger")
        return redirect("/login")

    except Exception as e:
        db.rollback()
        flash(f"登入失敗：{e}", "danger")
        return redirect("/login")


@auth_bp.get("/forgot-password")
def forgot_password_page():
    return render_template("mobile/auth/forgot_password.html")


@auth_bp.post("/forgot-password")
def forgot_password_submit():
    db = get_db()
    email = normalize_email(request.form.get("email", ""))

    generic_message = "如果 email 存在，系統會寄出重設密碼連結。"

    try:
        token_data = create_password_reset_token(db, email)

        if token_data:
            reset_url = _reset_url(token_data["raw_token"])
            sent = send_password_reset_email(
                email,
                reset_url,
                user_id=token_data["user"]["id"],
            )

            if not sent:
                print("[AUTH][RESET] SMTP not configured or send failed.")
                if current_app.config.get("APP_ENV") != "production":
                    print("[AUTH][RESET][DEV] reset_url created but not displayed in production.")

        flash(generic_message, "success")
        return redirect("/forgot-password")

    except AuthError:
        flash(generic_message, "success")
        return redirect("/forgot-password")

    except Exception as exc:
        db.rollback()
        print(f"[AUTH][RESET][ERROR] {exc}")
        flash(generic_message, "success")
        return redirect("/forgot-password")


@auth_bp.get("/reset-password")
def reset_password_page():
    db = get_db()
    token = request.args.get("token", "").strip()

    user = get_user_by_valid_reset_token(db, token)

    if not user:
        flash("連結已失效，請重新申請。", "danger")
        return redirect("/forgot-password")

    return render_template(
        "mobile/auth/reset_password.html",
        token=token,
    )


@auth_bp.post("/reset-password")
def reset_password_submit():
    db = get_db()

    token = request.form.get("token", "").strip()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    try:
        user = reset_password_with_token(
            db,
            raw_token=token,
            new_password=password,
            confirm_password=confirm_password,
        )

        create_auth_block(
            db,
            event_type="USER_PASSWORD_RESET",
            user=user,
            payload={
                "login_id": user["login_id"],
                "role": user["role"],
            },
            commit=True,
        )

        flash("密碼已更新，請重新登入。", "success")
        return redirect("/login")

    except AuthError as exc:
        flash(str(exc), "danger")
        return redirect(f"/reset-password?token={token}")

    except Exception as exc:
        db.rollback()
        flash(f"密碼更新失敗：{exc}", "danger")
        return redirect("/forgot-password")


@auth_bp.get("/account/password")
@login_required
def change_password_page():
    user = current_user()

    if user and user["role"] == "ADMIN_OPERATOR":
        flash("Admin 設定帳號目前不支援在此頁修改密碼。", "warning")
        return redirect(role_home(user["role"]))

    return render_template(
        "mobile/auth/change_password.html",
        user=user,
    )


@auth_bp.post("/account/password")
@login_required
def change_password_submit():
    db = get_db()
    user = current_user()

    if not user:
        flash("請重新登入。", "warning")
        return redirect("/login")

    if user["role"] == "ADMIN_OPERATOR":
        flash("Admin 設定帳號目前不支援在此頁修改密碼。", "warning")
        return redirect("/admin")

    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    try:
        updated_user = change_user_password(
            db,
            user_id=user["id"],
            current_password=current_password,
            new_password=new_password,
            confirm_password=confirm_password,
        )

        create_auth_block(
            db,
            event_type="USER_PASSWORD_CHANGED",
            user=updated_user,
            payload={
                "login_id": updated_user["login_id"],
                "role": updated_user["role"],
            },
            commit=True,
        )

        flash("密碼已更新。", "success")
        return redirect("/account/password")

    except AuthError as exc:
        flash(str(exc), "danger")
        return redirect("/account/password")

    except Exception as exc:
        db.rollback()
        flash(f"密碼更新失敗：{exc}", "danger")
        return redirect("/account/password")


@auth_bp.get("/logout")
def logout():
    session.clear()
    flash("已登出。", "success")
    return redirect("/")

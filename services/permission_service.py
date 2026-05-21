from functools import wraps

from flask import flash, redirect, request, session

from db import get_db
from services.auth_service import (
    get_driver_by_user_id,
    get_store_by_user_id,
    get_user_by_id,
)


def current_user():
    role = session.get("role", "")
    user_id = session.get("user_id")

    if role == "ADMIN_OPERATOR":
        return {
            "id": 0,
            "login_id": session.get("login_id", "admin"),
            "role": "ADMIN_OPERATOR",
            "display_name": session.get("display_name", "Admin"),
            "phone": "",
            "status": "ACTIVE",
        }

    if user_id is None:
        return None

    return get_user_by_id(get_db(), user_id)


def current_role():
    return session.get("role", "")


def is_logged_in():
    role = session.get("role", "")
    user_id = session.get("user_id")

    if role == "ADMIN_OPERATOR":
        return True

    return user_id is not None


def is_admin():
    return session.get("role") == "ADMIN_OPERATOR"


def current_target_code():
    return session.get("target_code", "")


def role_home(role=None):
    role = role or current_role()

    if role == "CUSTOMER":
        return "/show"

    if role == "STORE":
        return "/store"

    if role == "DRIVER":
        return "/driver"

    if role == "ADMIN_OPERATOR":
        return "/admin"

    return "/"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_logged_in():
            flash("請先登入。", "warning")
            return redirect(f"/login?next={request.path}")

        return view(*args, **kwargs)

    return wrapped


def role_required(*roles):
    allowed = {r.upper() for r in roles}

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not is_logged_in():
                flash("請先登入。", "warning")
                return redirect(f"/login?next={request.path}")

            role = current_role()

            if role not in allowed:
                flash("此頁面沒有權限。", "danger")
                return redirect(role_home(role))

            return view(*args, **kwargs)

        return wrapped

    return decorator


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_logged_in():
            flash("請先登入管理員。", "warning")
            return redirect(f"/login?next={request.path}")

        if not is_admin():
            flash("此頁面僅限 Admin。", "danger")
            return redirect(role_home())

        return view(*args, **kwargs)

    return wrapped


def get_current_store():
    if current_role() != "STORE":
        return None

    db = get_db()
    user_id = session.get("user_id")

    return get_store_by_user_id(db, user_id)


def get_current_driver():
    if current_role() != "DRIVER":
        return None

    db = get_db()
    user_id = session.get("user_id")

    return get_driver_by_user_id(db, user_id)


def require_store():
    store = get_current_store()

    if not store:
        return None

    return store


def require_driver():
    driver = get_current_driver()

    if not driver:
        return None

    return driver

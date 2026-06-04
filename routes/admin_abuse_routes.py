from flask import Blueprint, flash, redirect, render_template, request, session

from db import get_db
from services.abuse_guard import add_email_suppression, release_email_suppression
from services.block_service import create_block
from services.email_service import normalize_email
from services.permission_service import admin_required


admin_abuse_bp = Blueprint("admin_abuse", __name__, url_prefix="/admin/abuse")


def _safe_text(value, default=""):
    value = "" if value is None else str(value)
    value = value.strip()
    return value if value else default


def _int(value, default=0):
    try:
        return int(value or 0)
    except Exception:
        return int(default or 0)


def _admin_actor():
    try:
        return int(session.get("user_id") or 0), session.get("login_id") or "admin"
    except Exception:
        return 0, "admin"


def _get_user(db, user_id):
    return db.execute(
        """
        SELECT *
        FROM users
        WHERE id = ?
        LIMIT 1
        """,
        (int(user_id or 0),),
    ).fetchone()


def _user_order_count(db, user_id):
    row = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM orders
        WHERE customer_user_id = ?
           OR store_created_by = ?
        """,
        (int(user_id or 0), int(user_id or 0)),
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def _unverified_users(db):
    return db.execute(
        """
        SELECT u.*,
               COALESCE((
                 SELECT COUNT(*)
                 FROM email_logs el
                 WHERE lower(COALESCE(el.recipient_email, '')) = lower(COALESCE(u.email, ''))
                   AND el.status = 'FAILED'
               ), 0) AS failed_email_count,
               COALESCE((
                 SELECT COUNT(*)
                 FROM orders o
                 WHERE o.customer_user_id = u.id
                    OR o.store_created_by = u.id
               ), 0) AS order_count
        FROM users u
        WHERE u.role IN ('CUSTOMER', 'STORE', 'DRIVER')
          AND COALESCE(u.email, '') != ''
          AND u.email_verified_at IS NULL
          AND COALESCE(u.status, 'ACTIVE') != 'DELETED'
        ORDER BY u.id DESC
        LIMIT 500
        """
    ).fetchall()


def _ip_stats(db):
    return db.execute(
        """
        SELECT COALESCE(NULLIF(register_ip, ''), 'UNKNOWN') AS register_ip,
               COUNT(*) AS user_count,
               SUM(CASE WHEN email_verified_at IS NULL THEN 1 ELSE 0 END) AS unverified_count,
               MAX(created_at) AS latest_created_at
        FROM users
        WHERE role IN ('CUSTOMER', 'STORE', 'DRIVER')
        GROUP BY COALESCE(NULLIF(register_ip, ''), 'UNKNOWN')
        ORDER BY user_count DESC, latest_created_at DESC
        LIMIT 100
        """
    ).fetchall()


def _domain_stats(db):
    return db.execute(
        """
        SELECT lower(substr(email, instr(email, '@') + 1)) AS domain,
               COUNT(*) AS user_count,
               SUM(CASE WHEN email_verified_at IS NULL THEN 1 ELSE 0 END) AS unverified_count,
               MAX(created_at) AS latest_created_at
        FROM users
        WHERE COALESCE(email, '') != ''
          AND instr(email, '@') > 0
        GROUP BY lower(substr(email, instr(email, '@') + 1))
        ORDER BY user_count DESC, latest_created_at DESC
        LIMIT 100
        """
    ).fetchall()


def _email_stats(db):
    return db.execute(
        """
        SELECT lower(COALESCE(email, '')) AS email,
               COUNT(*) AS user_count,
               SUM(CASE WHEN email_verified_at IS NULL THEN 1 ELSE 0 END) AS unverified_count,
               MAX(created_at) AS latest_created_at
        FROM users
        WHERE COALESCE(email, '') != ''
        GROUP BY lower(COALESCE(email, ''))
        HAVING COUNT(*) >= 1
        ORDER BY user_count DESC, latest_created_at DESC
        LIMIT 100
        """
    ).fetchall()


def _suppressions(db):
    return db.execute(
        """
        SELECT *
        FROM email_suppressions
        WHERE status = 'ACTIVE'
        ORDER BY id DESC
        LIMIT 300
        """
    ).fetchall()


@admin_abuse_bp.get("")
@admin_abuse_bp.get("/")
@admin_required
def dashboard():
    db = get_db()

    summary_row = db.execute(
        """
        SELECT COUNT(*) AS total_users,
               SUM(CASE WHEN email_verified_at IS NULL AND COALESCE(email, '') != '' THEN 1 ELSE 0 END) AS unverified_email_users,
               SUM(CASE WHEN COALESCE(status, 'ACTIVE') != 'ACTIVE' THEN 1 ELSE 0 END) AS inactive_users
        FROM users
        WHERE role IN ('CUSTOMER', 'STORE', 'DRIVER')
        """
    ).fetchone()

    summary = {
        "total_users": _int(summary_row["total_users"] if summary_row else 0),
        "unverified_email_users": _int(summary_row["unverified_email_users"] if summary_row else 0),
        "inactive_users": _int(summary_row["inactive_users"] if summary_row else 0),
    }

    return render_template(
        "mobile/admin/abuse.html",
        summary=summary,
        unverified_users=_unverified_users(db),
        ip_stats=_ip_stats(db),
        domain_stats=_domain_stats(db),
        email_stats=_email_stats(db),
        suppressions=_suppressions(db),
    )


@admin_abuse_bp.post("/users/<int:user_id>/block")
@admin_required
def block_user(user_id):
    db = get_db()
    reason = _safe_text(request.form.get("reason", ""), "Admin marked user as spam")
    user = _get_user(db, user_id)

    if not user:
        flash("找不到使用者。", "danger")
        return redirect("/admin/abuse")

    if user["role"] == "ADMIN_OPERATOR":
        flash("不能在此停用 Admin 帳號。", "danger")
        return redirect("/admin/abuse")

    admin_user_id, admin_login_id = _admin_actor()

    try:
        db.execute(
            """
            UPDATE users
            SET status = 'SUSPENDED',
                updated_at = datetime('now', '+8 hours')
            WHERE id = ?
            """,
            (user_id,),
        )

        create_block(
            db,
            event_type="ADMIN_USER_BLOCKED",
            actor_role="ADMIN_OPERATOR",
            actor_id=admin_user_id,
            actor_code=admin_login_id,
            amount_twd=0,
            payload={
                "user_id": user_id,
                "login_id": user["login_id"],
                "role": user["role"],
                "email": user["email"] or "",
                "register_ip": user["register_ip"] if "register_ip" in user.keys() else "",
                "reason": reason,
            },
            commit=False,
        )
        db.commit()
        flash("已停用此使用者。", "success")
    except Exception as exc:
        db.rollback()
        flash(f"停用失敗：{exc}", "danger")

    return redirect("/admin/abuse")


@admin_abuse_bp.post("/users/<int:user_id>/delete")
@admin_required
def delete_user(user_id):
    db = get_db()
    user = _get_user(db, user_id)

    if not user:
        flash("找不到使用者。", "danger")
        return redirect("/admin/abuse")

    if user["role"] == "ADMIN_OPERATOR":
        flash("不能刪除 Admin 帳號。", "danger")
        return redirect("/admin/abuse")

    order_count = _user_order_count(db, user_id)

    if order_count > 0:
        flash("此使用者已有訂單紀錄，已改用停用處理以保留交易紀錄。", "warning")
        return redirect(f"/admin/abuse/users/{user_id}/block")

    admin_user_id, admin_login_id = _admin_actor()

    try:
        create_block(
            db,
            event_type="ADMIN_USER_DELETED",
            actor_role="ADMIN_OPERATOR",
            actor_id=admin_user_id,
            actor_code=admin_login_id,
            amount_twd=0,
            payload={
                "user_id": user_id,
                "login_id": user["login_id"],
                "role": user["role"],
                "email": user["email"] or "",
                "register_ip": user["register_ip"] if "register_ip" in user.keys() else "",
            },
            commit=False,
        )

        db.execute("DELETE FROM stores WHERE owner_user_id = ?", (user_id,))
        db.execute("DELETE FROM drivers WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.commit()
        flash("已刪除此未使用 spam 帳號。", "success")
    except Exception as exc:
        db.rollback()
        flash(f"刪除失敗：{exc}", "danger")

    return redirect("/admin/abuse")


@admin_abuse_bp.post("/suppressions")
@admin_required
def add_suppression():
    db = get_db()
    email = normalize_email(request.form.get("email", ""))
    reason = _safe_text(request.form.get("reason", ""), "Manual admin suppression")

    if not email:
        flash("請輸入 Email。", "warning")
        return redirect("/admin/abuse")

    try:
        add_email_suppression(db, email, reason=reason, source="admin_manual", commit=True)
        flash("已加入 Email suppression。", "success")
    except Exception as exc:
        db.rollback()
        flash(f"加入 suppression 失敗：{exc}", "danger")

    return redirect("/admin/abuse")


@admin_abuse_bp.post("/suppressions/release")
@admin_required
def release_suppression():
    db = get_db()
    email = normalize_email(request.form.get("email", ""))

    if not email:
        flash("請輸入 Email。", "warning")
        return redirect("/admin/abuse")

    try:
        release_email_suppression(db, email, commit=True)
        flash("已解除 Email suppression。", "success")
    except Exception as exc:
        db.rollback()
        flash(f"解除 suppression 失敗：{exc}", "danger")

    return redirect("/admin/abuse")

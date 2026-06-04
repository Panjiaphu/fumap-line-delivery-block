import csv
import io
from urllib.parse import urlencode

from flask import Blueprint, Response, flash, redirect, render_template, request, session

from db import get_db
from services.abuse_guard import add_email_suppression, release_email_suppression
from services.block_service import create_block
from services.bounce_service import bounce_summary, recent_bounces, sync_gmail_bounces
from services.email_service import normalize_email
from services.permission_service import admin_required


admin_abuse_bp = Blueprint("admin_abuse", __name__, url_prefix="/admin/abuse")


PER_PAGE_OPTIONS = {25, 50, 100, 200}


def _safe_text(value, default=""):
    value = "" if value is None else str(value)
    value = value.strip()
    return value if value else default


def _int(value, default=0):
    try:
        return int(value or 0)
    except Exception:
        return int(default or 0)


def _page():
    return max(1, _int(request.args.get("page"), 1))


def _per_page():
    value = _int(request.args.get("per_page"), 50)
    return value if value in PER_PAGE_OPTIONS else 50


def _filters():
    return {
        "role": _safe_text(request.args.get("role", "")).upper(),
        "q": _safe_text(request.args.get("q", "")),
        "ip": _safe_text(request.args.get("ip", "")),
        "domain": _safe_text(request.args.get("domain", "")).lower(),
        "page": _page(),
        "per_page": _per_page(),
    }


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


def _block_existing_user(db, user, reason):
    admin_user_id, admin_login_id = _admin_actor()
    user_id = int(user["id"])

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


def _delete_or_suspend_user(db, user, reason):
    user_id = int(user["id"])

    if user["role"] == "ADMIN_OPERATOR":
        return "skipped_admin"

    order_count = _user_order_count(db, user_id)

    if order_count > 0:
        _block_existing_user(db, user, reason or "Bulk delete requested but user has orders; suspended instead")
        return "suspended_has_orders"

    admin_user_id, admin_login_id = _admin_actor()

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
            "reason": reason,
        },
        commit=False,
    )

    db.execute("DELETE FROM stores WHERE owner_user_id = ?", (user_id,))
    db.execute("DELETE FROM drivers WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return "deleted"


def _unverified_where(filters):
    where = [
        "u.role IN ('CUSTOMER', 'STORE', 'DRIVER')",
        "COALESCE(u.email, '') != ''",
        "u.email_verified_at IS NULL",
        "COALESCE(u.status, 'ACTIVE') != 'DELETED'",
    ]
    params = []

    if filters.get("role") in {"CUSTOMER", "STORE", "DRIVER"}:
        where.append("u.role = ?")
        params.append(filters["role"])

    if filters.get("ip"):
        where.append("COALESCE(u.register_ip, '') = ?")
        params.append(filters["ip"])

    if filters.get("domain"):
        where.append("lower(substr(u.email, instr(u.email, '@') + 1)) = ?")
        params.append(filters["domain"])

    if filters.get("q"):
        where.append(
            "(lower(COALESCE(u.email, '')) LIKE ? OR lower(COALESCE(u.login_id, '')) LIKE ? OR lower(COALESCE(u.display_name, '')) LIKE ? OR COALESCE(u.phone, '') LIKE ?)"
        )
        like = f"%{filters['q'].lower()}%"
        params.extend([like, like, like, f"%{filters['q']}%"])

    return "WHERE " + " AND ".join(where), params


def _unverified_users(db, filters):
    where_sql, params = _unverified_where(filters)
    limit = int(filters["per_page"])
    offset = (int(filters["page"]) - 1) * limit

    return db.execute(
        f"""
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
        {where_sql}
        ORDER BY u.id DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()


def _unverified_count(db, filters):
    where_sql, params = _unverified_where(filters)
    row = db.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM users u
        {where_sql}
        """,
        params,
    ).fetchone()
    return int(row["c"] or 0) if row else 0


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


def _register_day_chart(db):
    return db.execute(
        """
        SELECT substr(COALESCE(created_at, ''), 1, 10) AS bucket,
               COUNT(*) AS user_count,
               SUM(CASE WHEN email_verified_at IS NULL AND COALESCE(email, '') != '' THEN 1 ELSE 0 END) AS unverified_count
        FROM users
        WHERE role IN ('CUSTOMER', 'STORE', 'DRIVER')
          AND COALESCE(created_at, '') != ''
        GROUP BY substr(COALESCE(created_at, ''), 1, 10)
        ORDER BY bucket DESC
        LIMIT 14
        """
    ).fetchall()


def _register_hour_chart(db):
    return db.execute(
        """
        SELECT substr(COALESCE(created_at, ''), 12, 2) AS bucket,
               COUNT(*) AS user_count,
               SUM(CASE WHEN email_verified_at IS NULL AND COALESCE(email, '') != '' THEN 1 ELSE 0 END) AS unverified_count
        FROM users
        WHERE role IN ('CUSTOMER', 'STORE', 'DRIVER')
          AND COALESCE(created_at, '') != ''
        GROUP BY substr(COALESCE(created_at, ''), 12, 2)
        ORDER BY bucket
        """
    ).fetchall()


def _pagination(filters, total):
    page = int(filters["page"])
    per_page = int(filters["per_page"])
    total_pages = max(1, (int(total or 0) + per_page - 1) // per_page)
    base = {k: v for k, v in filters.items() if k not in {"page"} and v}

    return {
        "page": page,
        "per_page": per_page,
        "total": int(total or 0),
        "total_pages": total_pages,
        "prev_url": f"/admin/abuse?{urlencode({**base, 'page': max(1, page - 1)})}" if page > 1 else "",
        "next_url": f"/admin/abuse?{urlencode({**base, 'page': min(total_pages, page + 1)})}" if page < total_pages else "",
        "export_url": f"/admin/abuse/export/unverified?{urlencode(base)}",
    }


def _csv_response(filename, rows, columns):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)

    for row in rows:
        writer.writerow([row[col] if col in row.keys() else "" for col in columns])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@admin_abuse_bp.get("")
@admin_abuse_bp.get("/")
@admin_required
def dashboard():
    db = get_db()
    filters = _filters()
    total_unverified = _unverified_count(db, filters)

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
    summary.update(bounce_summary(db))

    return render_template(
        "mobile/admin/abuse.html",
        filters=filters,
        pagination=_pagination(filters, total_unverified),
        summary=summary,
        unverified_users=_unverified_users(db, filters),
        ip_stats=_ip_stats(db),
        domain_stats=_domain_stats(db),
        email_stats=_email_stats(db),
        suppressions=_suppressions(db),
        register_day_chart=_register_day_chart(db),
        register_hour_chart=_register_hour_chart(db),
        bounces=recent_bounces(db, limit=100),
    )


@admin_abuse_bp.post("/bulk")
@admin_required
def bulk_action():
    db = get_db()
    action = _safe_text(request.form.get("action", ""))
    user_ids = [int(item) for item in request.form.getlist("user_ids") if str(item).isdigit()]

    if not user_ids:
        flash("請先選擇帳號。", "warning")
        return redirect(request.referrer or "/admin/abuse")

    counts = {"blocked": 0, "deleted": 0, "suspended": 0, "suppressed": 0, "skipped": 0}

    try:
        for user_id in user_ids:
            user = _get_user(db, user_id)

            if not user or user["role"] == "ADMIN_OPERATOR":
                counts["skipped"] += 1
                continue

            if action == "block":
                _block_existing_user(db, user, "Bulk admin spam block")
                counts["blocked"] += 1
            elif action == "delete":
                result = _delete_or_suspend_user(db, user, "Bulk admin spam delete")
                if result == "deleted":
                    counts["deleted"] += 1
                elif result == "suspended_has_orders":
                    counts["suspended"] += 1
                else:
                    counts["skipped"] += 1
            elif action == "suppress":
                email = normalize_email(user["email"] if "email" in user.keys() else "")
                if email:
                    add_email_suppression(db, email, reason="Bulk admin spam suppression", source="admin_bulk", commit=False)
                    counts["suppressed"] += 1
                else:
                    counts["skipped"] += 1
            else:
                flash("Bulk action 不支援。", "danger")
                return redirect(request.referrer or "/admin/abuse")

        db.commit()
        flash(
            f"Bulk 完成：block {counts['blocked']}，delete {counts['deleted']}，suspend {counts['suspended']}，suppression {counts['suppressed']}，skip {counts['skipped']}。",
            "success",
        )
    except Exception as exc:
        db.rollback()
        flash(f"Bulk action 失敗：{exc}", "danger")

    return redirect(request.referrer or "/admin/abuse")


@admin_abuse_bp.post("/bounces/sync")
@admin_required
def sync_bounces():
    db = get_db()
    result = sync_gmail_bounces(db)

    if result.get("ok"):
        flash(
            f"Bounce sync 完成：checked {result['checked']}，processed {result['processed']}，suppressed {result['suppressed']}。",
            "success",
        )
    else:
        flash(f"Bounce sync 失敗：{result.get('error') or 'unknown error'}", "danger")

    return redirect("/admin/abuse")


@admin_abuse_bp.get("/export/<kind>")
@admin_required
def export_csv(kind):
    db = get_db()

    if kind == "unverified":
        filters = _filters()
        filters["page"] = 1
        filters["per_page"] = 10000
        rows = _unverified_users(db, filters)
        columns = ["id", "login_id", "role", "email", "display_name", "phone", "register_ip", "user_agent", "created_at", "verify_send_count", "failed_email_count", "order_count"]
        return _csv_response("fumap-unverified-users.csv", rows, columns)

    if kind == "ip":
        return _csv_response("fumap-register-ip-stats.csv", _ip_stats(db), ["register_ip", "user_count", "unverified_count", "latest_created_at"])

    if kind == "domain":
        return _csv_response("fumap-email-domain-stats.csv", _domain_stats(db), ["domain", "user_count", "unverified_count", "latest_created_at"])

    if kind == "email":
        return _csv_response("fumap-email-stats.csv", _email_stats(db), ["email", "user_count", "unverified_count", "latest_created_at"])

    if kind == "bounces":
        return _csv_response("fumap-email-bounces.csv", recent_bounces(db, limit=10000), ["id", "recipient_email", "message_subject", "sender", "message_date", "bounce_reason", "source", "created_at"])

    flash("Export 類型不支援。", "danger")
    return redirect("/admin/abuse")


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

    try:
        _block_existing_user(db, user, reason)
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

    try:
        result = _delete_or_suspend_user(db, user, "Admin spam delete")
        db.commit()

        if result == "deleted":
            flash("已刪除此未使用 spam 帳號。", "success")
        elif result == "suspended_has_orders":
            flash("此使用者已有訂單紀錄，已改為停用以保留交易紀錄。", "warning")
        else:
            flash("此帳號不能刪除。", "warning")
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

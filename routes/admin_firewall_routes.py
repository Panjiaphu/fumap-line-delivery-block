import csv
import io
from urllib.parse import urlencode

from flask import Blueprint, Response, flash, redirect, render_template, request

from db import get_db
from services.firewall_service import add_ip_block, release_ip_block
from services.permission_service import admin_required


admin_firewall_bp = Blueprint("admin_firewall", __name__, url_prefix="/admin/firewall")
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


def _filters():
    per_page = _int(request.args.get("per_page"), 50)
    return {
        "role": _safe_text(request.args.get("role", "")).upper(),
        "attack_type": _safe_text(request.args.get("attack_type", "")).upper(),
        "ip": _safe_text(request.args.get("ip", "")),
        "area": _safe_text(request.args.get("area", "")),
        "page": max(1, _int(request.args.get("page"), 1)),
        "per_page": per_page if per_page in PER_PAGE_OPTIONS else 50,
    }


def _where(filters):
    where = []
    params = []

    if filters["role"]:
        where.append("account_role = ?")
        params.append(filters["role"])

    if filters["attack_type"]:
        where.append("attack_type = ?")
        params.append(filters["attack_type"])

    if filters["ip"]:
        where.append("ip_address = ?")
        params.append(filters["ip"])

    if filters["area"]:
        where.append("area_hint LIKE ?")
        params.append(f"%{filters['area']}%")

    where_sql = "WHERE " + " AND ".join(where) if where else ""
    return where_sql, params


def _summary(db):
    row = db.execute(
        """
        SELECT COUNT(*) AS total_events,
               SUM(CASE WHEN account_role = 'HACKER' THEN 1 ELSE 0 END) AS hacker_events,
               SUM(CASE WHEN severity = 'HIGH' THEN 1 ELSE 0 END) AS high_events,
               SUM(CASE WHEN action_taken IN ('BLOCKED', 'AUTO_BLOCKED') THEN 1 ELSE 0 END) AS blocked_events
        FROM firewall_events
        """
    ).fetchone()

    active_blocks = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM firewall_ip_blocks
        WHERE status = 'ACTIVE'
        """
    ).fetchone()

    return {
        "total_events": _int(row["total_events"] if row else 0),
        "hacker_events": _int(row["hacker_events"] if row else 0),
        "high_events": _int(row["high_events"] if row else 0),
        "blocked_events": _int(row["blocked_events"] if row else 0),
        "active_blocks": _int(active_blocks["c"] if active_blocks else 0),
    }


def _event_count(db, filters):
    where_sql, params = _where(filters)
    row = db.execute(
        f"""
        SELECT COUNT(*) AS c
        FROM firewall_events
        {where_sql}
        """,
        params,
    ).fetchone()
    return int(row["c"] or 0) if row else 0


def _recent_events(db, filters):
    where_sql, params = _where(filters)
    limit = int(filters["per_page"])
    offset = (int(filters["page"]) - 1) * limit
    return db.execute(
        f"""
        SELECT *
        FROM firewall_events
        {where_sql}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()


def _ip_rollup(db):
    return db.execute(
        """
        SELECT fe.ip_address,
               fe.account_role,
               COALESCE(NULLIF(fe.area_hint, ''), 'UNKNOWN') AS area_hint,
               COUNT(*) AS event_count,
               SUM(CASE WHEN fe.severity = 'HIGH' THEN 1 ELSE 0 END) AS high_count,
               MAX(fe.created_at) AS latest_seen,
               GROUP_CONCAT(DISTINCT fe.attack_type) AS attack_types,
               MAX(fe.mitigation_hint) AS mitigation_hint,
               CASE WHEN fb.id IS NULL THEN 0 ELSE 1 END AS is_blocked
        FROM firewall_events fe
        LEFT JOIN firewall_ip_blocks fb
          ON fb.ip_address = fe.ip_address
         AND fb.status = 'ACTIVE'
        GROUP BY fe.ip_address, fe.account_role, COALESCE(NULLIF(fe.area_hint, ''), 'UNKNOWN')
        ORDER BY high_count DESC, event_count DESC, latest_seen DESC
        LIMIT 200
        """
    ).fetchall()


def _role_stats(db):
    return db.execute(
        """
        SELECT account_role,
               COUNT(*) AS event_count,
               COUNT(DISTINCT ip_address) AS ip_count,
               SUM(CASE WHEN severity = 'HIGH' THEN 1 ELSE 0 END) AS high_count
        FROM firewall_events
        GROUP BY account_role
        ORDER BY event_count DESC
        """
    ).fetchall()


def _area_stats(db):
    return db.execute(
        """
        SELECT COALESCE(NULLIF(area_hint, ''), 'UNKNOWN') AS area_hint,
               COUNT(*) AS event_count,
               COUNT(DISTINCT ip_address) AS ip_count,
               SUM(CASE WHEN account_role = 'HACKER' THEN 1 ELSE 0 END) AS hacker_count
        FROM firewall_events
        GROUP BY COALESCE(NULLIF(area_hint, ''), 'UNKNOWN')
        ORDER BY event_count DESC
        LIMIT 100
        """
    ).fetchall()


def _attack_stats(db):
    return db.execute(
        """
        SELECT attack_type,
               severity,
               COUNT(*) AS event_count,
               COUNT(DISTINCT ip_address) AS ip_count,
               MAX(mitigation_hint) AS mitigation_hint
        FROM firewall_events
        GROUP BY attack_type, severity
        ORDER BY event_count DESC
        LIMIT 100
        """
    ).fetchall()


def _active_blocks(db):
    return db.execute(
        """
        SELECT *
        FROM firewall_ip_blocks
        WHERE status = 'ACTIVE'
        ORDER BY id DESC
        LIMIT 200
        """
    ).fetchall()


def _day_chart(db):
    return db.execute(
        """
        SELECT substr(COALESCE(created_at, ''), 1, 10) AS bucket,
               COUNT(*) AS event_count,
               SUM(CASE WHEN severity = 'HIGH' THEN 1 ELSE 0 END) AS high_count
        FROM firewall_events
        WHERE COALESCE(created_at, '') != ''
        GROUP BY substr(COALESCE(created_at, ''), 1, 10)
        ORDER BY bucket DESC
        LIMIT 14
        """
    ).fetchall()


def _hour_chart(db):
    return db.execute(
        """
        SELECT substr(COALESCE(created_at, ''), 12, 2) AS bucket,
               COUNT(*) AS event_count,
               SUM(CASE WHEN severity = 'HIGH' THEN 1 ELSE 0 END) AS high_count
        FROM firewall_events
        WHERE COALESCE(created_at, '') != ''
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
        "prev_url": f"/admin/firewall?{urlencode({**base, 'page': max(1, page - 1)})}" if page > 1 else "",
        "next_url": f"/admin/firewall?{urlencode({**base, 'page': min(total_pages, page + 1)})}" if page < total_pages else "",
        "export_url": f"/admin/firewall/export/events?{urlencode(base)}",
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


@admin_firewall_bp.get("")
@admin_firewall_bp.get("/")
@admin_required
def dashboard():
    db = get_db()
    filters = _filters()
    total_events = _event_count(db, filters)

    return render_template(
        "mobile/admin/firewall.html",
        filters=filters,
        pagination=_pagination(filters, total_events),
        summary=_summary(db),
        recent_events=_recent_events(db, filters),
        ip_rollup=_ip_rollup(db),
        role_stats=_role_stats(db),
        area_stats=_area_stats(db),
        attack_stats=_attack_stats(db),
        active_blocks=_active_blocks(db),
        day_chart=_day_chart(db),
        hour_chart=_hour_chart(db),
    )


@admin_firewall_bp.post("/bulk")
@admin_required
def bulk_action():
    db = get_db()
    action = _safe_text(request.form.get("action", ""))
    ips = [_safe_text(item) for item in request.form.getlist("ip_addresses") if _safe_text(item)]

    if not ips:
        flash("請先選擇 IP。", "warning")
        return redirect(request.referrer or "/admin/firewall")

    try:
        count = 0
        for ip in ips:
            if action == "block":
                add_ip_block(db, ip, reason="Bulk admin firewall block", source="admin_bulk", commit=False)
                count += 1
            elif action == "release":
                count += release_ip_block(db, ip, commit=False)
            else:
                flash("Bulk action 不支援。", "danger")
                return redirect(request.referrer or "/admin/firewall")

        db.commit()
        flash(f"Bulk firewall action 完成：{count} IP。", "success")
    except Exception as exc:
        db.rollback()
        flash(f"Bulk firewall action 失敗：{exc}", "danger")

    return redirect(request.referrer or "/admin/firewall")


@admin_firewall_bp.get("/export/<kind>")
@admin_required
def export_csv(kind):
    db = get_db()

    if kind == "events":
        filters = _filters()
        filters["page"] = 1
        filters["per_page"] = 10000
        rows = _recent_events(db, filters)
        columns = ["id", "created_at", "ip_address", "account_role", "account_id", "account_label", "area_hint", "attack_type", "severity", "action_taken", "method", "path", "query_string", "user_agent", "mitigation_hint"]
        return _csv_response("fumap-firewall-events.csv", rows, columns)

    if kind == "ips":
        return _csv_response("fumap-firewall-ip-rollup.csv", _ip_rollup(db), ["ip_address", "account_role", "area_hint", "event_count", "high_count", "latest_seen", "attack_types", "mitigation_hint", "is_blocked"])

    if kind == "areas":
        return _csv_response("fumap-firewall-area-stats.csv", _area_stats(db), ["area_hint", "event_count", "ip_count", "hacker_count"])

    if kind == "attacks":
        return _csv_response("fumap-firewall-attack-stats.csv", _attack_stats(db), ["attack_type", "severity", "event_count", "ip_count", "mitigation_hint"])

    flash("Export 類型不支援。", "danger")
    return redirect("/admin/firewall")


@admin_firewall_bp.post("/blocks")
@admin_required
def block_ip():
    db = get_db()
    ip_address = _safe_text(request.form.get("ip_address", ""))
    reason = _safe_text(request.form.get("reason", ""), "Manual firewall block")

    if not ip_address:
        flash("請輸入 IP。", "warning")
        return redirect("/admin/firewall")

    try:
        add_ip_block(db, ip_address, reason=reason, source="admin_manual", commit=True)
        flash("已加入防火牆封鎖。", "success")
    except Exception as exc:
        db.rollback()
        flash(f"封鎖失敗：{exc}", "danger")

    return redirect("/admin/firewall")


@admin_firewall_bp.post("/blocks/release")
@admin_required
def unblock_ip():
    db = get_db()
    ip_address = _safe_text(request.form.get("ip_address", ""))

    if not ip_address:
        flash("請輸入 IP。", "warning")
        return redirect("/admin/firewall")

    try:
        release_ip_block(db, ip_address, commit=True)
        flash("已解除防火牆封鎖。", "success")
    except Exception as exc:
        db.rollback()
        flash(f"解除封鎖失敗：{exc}", "danger")

    return redirect("/admin/firewall")

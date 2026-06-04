from flask import Blueprint, flash, redirect, render_template, request

from db import get_db
from services.firewall_service import add_ip_block, release_ip_block
from services.permission_service import admin_required


admin_firewall_bp = Blueprint("admin_firewall", __name__, url_prefix="/admin/firewall")


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
    return {
        "role": _safe_text(request.args.get("role", "")).upper(),
        "attack_type": _safe_text(request.args.get("attack_type", "")).upper(),
        "ip": _safe_text(request.args.get("ip", "")),
        "area": _safe_text(request.args.get("area", "")),
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


def _recent_events(db, filters):
    where_sql, params = _where(filters)
    return db.execute(
        f"""
        SELECT *
        FROM firewall_events
        {where_sql}
        ORDER BY id DESC
        LIMIT 500
        """,
        params,
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


@admin_firewall_bp.get("")
@admin_firewall_bp.get("/")
@admin_required
def dashboard():
    db = get_db()
    filters = _filters()

    return render_template(
        "mobile/admin/firewall.html",
        filters=filters,
        summary=_summary(db),
        recent_events=_recent_events(db, filters),
        ip_rollup=_ip_rollup(db),
        role_stats=_role_stats(db),
        area_stats=_area_stats(db),
        attack_stats=_attack_stats(db),
        active_blocks=_active_blocks(db),
    )


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

import os

from flask import Blueprint, current_app, render_template

from db import get_db
from services.permission_service import admin_required


admin_timeblock_bp = Blueprint("admin_timeblock", __name__, url_prefix="/admin")


def _table_count(db, table_name, where_sql="", params=None):
    params = params or []
    sql = f"SELECT COUNT(*) AS c FROM {table_name}"

    if where_sql:
        sql += f" WHERE {where_sql}"

    try:
        row = db.execute(sql, params).fetchone()
        return int(row["c"] or 0) if row else 0
    except Exception:
        return 0


def _recent_timeblock_candidate_blocks(db, limit=20):
    try:
        return db.execute(
            """
            SELECT *
            FROM blocks
            WHERE event_type LIKE '%BLOCK%'
               OR event_type LIKE '%FGO%'
               OR event_type LIKE '%TIME%'
               OR event_type LIKE '%REWARD%'
               OR event_type LIKE '%ADMIN%'
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit or 20),),
        ).fetchall()
    except Exception:
        return []


def _safe_env_value(key, default=""):
    value = os.getenv(key, default)
    return (value or "").strip()


@admin_timeblock_bp.get("/timeblock")
@admin_required
def timeblock_console():
    """
    Phase 4 admin control surface for Timeblock / FGO.

    This page is intentionally read-only. Reward and conversion writes must later
    go through a service/API layer with source_project validation, idempotency,
    role guard, and audit logs. It must not write directly to the local DB.
    """
    db = get_db()

    timeblock_webapp_url = _safe_env_value(
        "TIMEBLOCK_WEBAPP_URL",
        current_app.config.get("TIMEBLOCK_WEBAPP_URL", "https://fumap-bot-life.onrender.com"),
    ).rstrip("/")

    timeblock_api_base_url = _safe_env_value(
        "TIMEBLOCK_API_BASE_URL",
        current_app.config.get("TIMEBLOCK_API_BASE_URL", ""),
    ).rstrip("/")

    project_code = _safe_env_value(
        "TIMEBLOCK_PROJECT_CODE",
        current_app.config.get("TIMEBLOCK_PROJECT_CODE", "fumapgo"),
    ) or "fumapgo"

    project_token_configured = bool(_safe_env_value("TIMEBLOCK_PROJECT_TOKEN", ""))

    integration = {
        "project_code": project_code,
        "source_project": "fumapgo",
        "timeblock_webapp_url": timeblock_webapp_url,
        "timeblock_api_base_url": timeblock_api_base_url,
        "project_token_configured": project_token_configured,
        "wallet_query_ready": bool(timeblock_api_base_url and project_token_configured),
        "event_ingestion_ready": bool(timeblock_api_base_url and project_token_configured),
        "conversion_boundary": "Timeblock owns points-to-FGO conversion; FUMAP GO only links and queries.",
    }

    summary = {
        "local_blocks": _table_count(db, "blocks"),
        "orders": _table_count(db, "orders"),
        "customers": _table_count(db, "users", "role = 'CUSTOMER'"),
        "stores": _table_count(db, "stores"),
        "drivers": _table_count(db, "drivers"),
        "online_drivers": _table_count(db, "drivers", "is_online = 1"),
        "open_stores": _table_count(db, "stores", "is_open = 1"),
    }

    reward_boundaries = [
        {
            "name": "Customer order reward",
            "owner": "FUMAP GO validates order evidence; Timeblock records internal points.",
            "status": "planned",
        },
        {
            "name": "Store availability reward",
            "owner": "FUMAP GO must validate heartbeat/session/cap first; Timeblock records accepted event.",
            "status": "planned",
        },
        {
            "name": "Shiper availability reward",
            "owner": "FUMAP GO must validate heartbeat/session/cap first; Timeblock records accepted event.",
            "status": "planned",
        },
        {
            "name": "Timeblock points to FGO conversion",
            "owner": "Timeblock owns conversion and future blockchain gateway.",
            "status": "external-timeblock",
        },
    ]

    required_apis = [
        "POST /api/projects/fumapgo/events",
        "GET /api/projects/fumapgo/wallets/{external_user_id}/summary",
        "GET /api/projects/fumapgo/wallets/{external_user_id}/blocks",
        "GET /api/projects/fumapgo/integration-health",
        "POST /api/projects/fumapgo/conversions/fgo/request",
    ]

    recent_blocks = _recent_timeblock_candidate_blocks(db, limit=20)

    return render_template(
        "mobile/admin/timeblock.html",
        integration=integration,
        summary=summary,
        reward_boundaries=reward_boundaries,
        required_apis=required_apis,
        recent_blocks=recent_blocks,
    )

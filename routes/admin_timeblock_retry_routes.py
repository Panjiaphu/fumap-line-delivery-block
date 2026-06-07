from flask import Blueprint, jsonify, request

from db import get_db
from services.permission_service import admin_required
from services.timeblock_retry_service import retry_pending_events


admin_timeblock_retry_bp = Blueprint(
    "admin_timeblock_retry",
    __name__,
    url_prefix="/admin/timeblock",
)


@admin_timeblock_retry_bp.post("/retry-pending")
@admin_required
def retry_pending_timeblock_events():
    data = request.get_json(silent=True) or {}
    try:
        limit = int(data.get("limit") or 20)
    except Exception:
        limit = 20

    limit = max(1, min(limit, 100))
    result = retry_pending_events(get_db(), limit=limit)

    counts = {
        "forwarded": 0,
        "retry_pending": 0,
        "dead_letter": 0,
    }

    for item in result.get("results", []):
        status = ((item.get("result") or {}).get("status") or "").lower()
        if status == "forwarded":
            counts["forwarded"] += 1
        elif status == "dead_letter":
            counts["dead_letter"] += 1
        else:
            counts["retry_pending"] += 1

    result.update(counts)
    return jsonify(result)

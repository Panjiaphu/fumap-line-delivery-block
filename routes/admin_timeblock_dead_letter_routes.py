from flask import Blueprint, jsonify, request

from db import get_db
from services.permission_service import admin_required
from services.timeblock_dead_letter_service import list_dead_letters, retry_dead_letter_now


admin_timeblock_dead_letter_bp = Blueprint(
    "admin_timeblock_dead_letter",
    __name__,
    url_prefix="/admin/timeblock",
)


@admin_timeblock_dead_letter_bp.get("/dead-letters")
@admin_required
def dead_letters():
    try:
        limit = int(request.args.get("limit") or 50)
    except Exception:
        limit = 50
    limit = max(1, min(limit, 100))
    rows = list_dead_letters(get_db(), limit=limit)
    return jsonify({"ok": True, "count": len(rows), "items": rows})


@admin_timeblock_dead_letter_bp.post("/dead-letter/<int:event_id>/retry")
@admin_required
def retry_dead_letter(event_id):
    result = retry_dead_letter_now(get_db(), event_id)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status

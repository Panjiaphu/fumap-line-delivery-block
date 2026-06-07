from flask import Blueprint, jsonify, request

from db import get_db
from services.permission_service import admin_required
from services.timeblock_review_admin_service import (
    approve_review,
    pending_reviews,
    reject_review,
    review_metrics,
)


admin_timeblock_review_bp = Blueprint(
    "admin_timeblock_review",
    __name__,
    url_prefix="/admin/timeblock",
)


@admin_timeblock_review_bp.get("/reviews/pending")
@admin_required
def pending_timeblock_reviews():
    try:
        limit = int(request.args.get("limit") or 50)
    except Exception:
        limit = 50
    rows = pending_reviews(get_db(), limit=limit)
    return jsonify({"ok": True, "count": len(rows), "items": rows})


@admin_timeblock_review_bp.get("/reviews/metrics")
@admin_required
def timeblock_review_metrics():
    return jsonify({"ok": True, "metrics": review_metrics(get_db())})


@admin_timeblock_review_bp.post("/review/<int:review_id>/approve")
@admin_required
def approve_timeblock_review(review_id):
    result = approve_review(get_db(), review_id)
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@admin_timeblock_review_bp.post("/review/<int:review_id>/reject")
@admin_required
def reject_timeblock_review(review_id):
    data = request.get_json(silent=True) or {}
    result = reject_review(get_db(), review_id, reason=data.get("reason") or "")
    status = 200 if result.get("ok") else 400
    return jsonify(result), status

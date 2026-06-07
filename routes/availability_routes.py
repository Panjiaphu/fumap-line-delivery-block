from flask import Blueprint, jsonify, request, session

from db import get_db
from services.availability_core import as_dict, ping, row_by_id, row_by_key, session_key
from services.permission_service import is_logged_in


availability_bp = Blueprint("availability", __name__, url_prefix="/api/availability")


def _auth_ok():
    return is_logged_in() or bool((request.headers.get("Authorization") or "").strip())


def _payload():
    return request.get_json(silent=True) or {}


@availability_bp.post("/heartbeat")
def heartbeat():
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = _payload()
    role = data.get("actor_role") or session.get("role")
    actor_id = data.get("actor_external_id") or session.get("target_code") or session.get("user_id")
    client_id = data.get("client_session_id") or "default"

    try:
        row, created = ping(get_db(), role, actor_id, client_id)
        return jsonify({"ok": True, "created": created, "session": as_dict(row)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@availability_bp.get("/session/<int:session_id>")
def session_detail(session_id):
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    row = row_by_id(get_db(), session_id)
    if not row:
        return jsonify({"ok": False, "error": "session not found"}), 404
    return jsonify({"ok": True, "session": as_dict(row)})


@availability_bp.post("/close")
def close_session():
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = _payload()
    role = data.get("actor_role") or session.get("role")
    actor_id = data.get("actor_external_id") or session.get("target_code") or session.get("user_id")
    client_id = data.get("client_session_id") or "default"
    key = data.get("session_key") or session_key(role, actor_id, client_id)

    db = get_db()
    row = row_by_key(db, key)
    if not row:
        return jsonify({"ok": False, "error": "session not found"}), 404
    if row["status"] != "ACTIVE":
        return jsonify({"ok": True, "session": as_dict(row)})

    db.execute("UPDATE availability_sessions SET status = 'CLOSED', ended_at = datetime('now'), updated_at = datetime('now') WHERE id = ?", (row["id"],))
    db.commit()
    row = row_by_key(db, key)
    return jsonify({"ok": True, "session": as_dict(row)})

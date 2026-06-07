from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session

from db import get_db
from services.availability_core import as_dict, ping, row_by_id, row_by_key, session_key
from services.permission_service import is_logged_in


availability_bp = Blueprint("availability", __name__, url_prefix="/api/availability")

MAX_IDLE_SECONDS = 180


def _auth_ok():
    return is_logged_in() or bool((request.headers.get("Authorization") or "").strip())


def _payload():
    return request.get_json(silent=True) or {}


def _parse_dt(value):
    value = (value or "").strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(value)
    except Exception:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out


def _minutes(row):
    started = _parse_dt(row["started_at"])
    seen = _parse_dt(row["last_seen_at"])
    if not started or not seen or seen < started:
        return 0
    return int((seen - started).total_seconds() // 60)


def _idle_seconds(row):
    seen = _parse_dt(row["last_seen_at"])
    if not seen:
        return 999999
    return int((datetime.now(timezone.utc) - seen).total_seconds())


def _close_state(row):
    minutes = _minutes(row)
    pings = int(row["ping_count"] or 0)
    notes = []
    if pings < 2:
        notes.append("ping_count_too_low")
    if minutes < 60:
        notes.append("below_60_minutes")
    if _idle_seconds(row) > MAX_IDLE_SECONDS:
        notes.append("heartbeat_timeout")
    if pings < 2 or "heartbeat_timeout" in notes:
        state = "REVIEW"
    elif minutes < 60:
        state = "NOT_ELIGIBLE"
    else:
        state = "CANDIDATE"
    points = 60 * (minutes // 60) if state == "CANDIDATE" else 0
    return state, minutes, points, ",".join(notes)


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

    state, minutes, points, note = _close_state(row)
    db.execute(
        """
        UPDATE availability_sessions
        SET status = ?, ended_at = datetime('now'), eligible_minutes = ?,
            reward_points = ?, review_reason = ?, updated_at = datetime('now')
        WHERE id = ?
        """,
        (state, minutes, points, note, row["id"]),
    )
    db.commit()
    row = row_by_key(db, key)
    return jsonify({"ok": True, "state": state, "session": as_dict(row)})

import hashlib
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, session

from db import get_db
from services.availability_core import as_dict, ping, row_by_id, row_by_key, session_key
from services.permission_service import is_logged_in
from services.reward_audit_snapshot_service import create_reward_snapshot
from services.timeblock_gateway_service import (
    forward_event_to_timeblock,
    get_outbound_event,
    record_outbound_event,
)


availability_bp = Blueprint("availability", __name__, url_prefix="/api/availability")

MAX_IDLE_SECONDS = 180
DAILY_CAP_MINUTES = 480


def _hash(value):
    value = (value or "").strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


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


def _active_count(db, row):
    out = db.execute(
        """
        SELECT COUNT(*) AS c
        FROM availability_sessions
        WHERE actor_role = ? AND actor_external_id = ? AND status = 'ACTIVE'
        """,
        (row["actor_role"], row["actor_external_id"]),
    ).fetchone()
    return int(out["c"] or 0) if out else 0


def _device_count(db, row):
    out = db.execute(
        """
        SELECT COUNT(DISTINCT device_fingerprint) AS c
        FROM availability_sessions
        WHERE actor_role = ? AND actor_external_id = ? AND status = 'ACTIVE'
          AND COALESCE(device_fingerprint, '') <> ''
        """,
        (row["actor_role"], row["actor_external_id"]),
    ).fetchone()
    return int(out["c"] or 0) if out else 0


def _used_today(db, row):
    today = datetime.now(timezone.utc).date().isoformat()
    out = db.execute(
        """
        SELECT COALESCE(SUM(eligible_minutes), 0) AS m
        FROM availability_sessions
        WHERE actor_role = ? AND actor_external_id = ?
          AND substr(COALESCE(ended_at, updated_at, created_at), 1, 10) = ?
          AND status = 'CANDIDATE'
        """,
        (row["actor_role"], row["actor_external_id"], today),
    ).fetchone()
    return int(out["m"] or 0) if out else 0


def _close_state(db, row):
    minutes = _minutes(row)
    pings = int(row["ping_count"] or 0)
    notes = []
    if pings < 2:
        notes.append("ping_count_too_low")
    if minutes < 60:
        notes.append("below_60_minutes")
    if _idle_seconds(row) > MAX_IDLE_SECONDS:
        notes.append("heartbeat_timeout")
    if _active_count(db, row) > 1:
        notes.append("duplicate_active_session")
    if _device_count(db, row) > 1:
        notes.append("multi_device_detected")
    remain = max(0, DAILY_CAP_MINUTES - _used_today(db, row))
    if remain <= 0:
        notes.append("daily_cap_reached")
        minutes = 0
    elif minutes > remain:
        notes.append("daily_cap_applied")
        minutes = remain
    if pings < 2 or "heartbeat_timeout" in notes or "duplicate_active_session" in notes or "multi_device_detected" in notes:
        state = "REVIEW"
    elif minutes < 60:
        state = "NOT_ELIGIBLE"
    else:
        state = "CANDIDATE"
    points = 60 * (minutes // 60) if state == "CANDIDATE" else 0
    return state, minutes, points, ",".join(notes)


def _availability_event(db, row):
    code = "STORE_ONLINE_REWARD_FINALIZED" if row["actor_role"] == "STORE" else "SHIPPER_ONLINE_REWARD_FINALIZED"
    event = {
        "source_project": "fumapgo",
        "event_code": code,
        "external_event_id": f"availability:{row['id']}",
        "idempotency_key": f"fumapgo:availability:{row['id']}",
        "actor_role": row["actor_role"],
        "actor_external_id": row["actor_external_id"],
        "points_delta": int(row["reward_points"] or 0),
        "occurred_at": row["ended_at"] or datetime.now(timezone.utc).isoformat(),
        "payload": {
            "session_id": row["id"],
            "session_key": row["session_key"],
            "eligible_minutes": int(row["eligible_minutes"] or 0),
            "contract_version": "v1",
        },
    }
    out, created = record_outbound_event(db, event)
    result = forward_event_to_timeblock(db, out) if created else {"duplicate": True}
    out = get_outbound_event(db, out["id"])
    return {"created": created, "forward_result": result, "outbound_event_id": out["id"], "status": out["status"]}


def _review_row(db, review_id):
    return db.execute(
        "SELECT * FROM availability_reward_reviews WHERE id = ? LIMIT 1",
        (review_id,),
    ).fetchone()


def _review_and_event(db, row):
    if row["status"] not in {"CANDIDATE", "REVIEW"}:
        return None

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    existing = db.execute(
        "SELECT * FROM availability_reward_reviews WHERE session_id = ? LIMIT 1",
        (row["id"],),
    ).fetchone()

    if existing and existing["outbound_event_id"]:
        return {"review_id": existing["id"], "status": existing["status"], "outbound_event_id": existing["outbound_event_id"]}

    if not existing:
        cur = db.execute(
            """
            INSERT INTO availability_reward_reviews
            (source_project, session_id, actor_role, actor_external_id, points_delta,
             status, review_reason, auto_review_result, created_at, updated_at)
            VALUES ('fumapgo', ?, ?, ?, ?, 'PENDING_REWARD', ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["actor_role"],
                row["actor_external_id"],
                int(row["reward_points"] or 0),
                row["review_reason"] or "",
                "CLEAN" if row["status"] == "CANDIDATE" else "RISK_FLAGS",
                now,
                now,
            ),
        )
        review_id = cur.lastrowid
    else:
        review_id = existing["id"]

    if row["status"] == "REVIEW":
        db.execute(
            """
            UPDATE availability_reward_reviews
            SET status = 'MANUAL_REVIEW_REQUIRED', auto_review_result = 'RISK_FLAGS',
                updated_at = ?
            WHERE id = ?
            """,
            (now, review_id),
        )
        db.commit()
        review = _review_row(db, review_id)
        snapshot_id = create_reward_snapshot(db, row, review, {"stage": "MANUAL_REVIEW_REQUIRED"})
        return {"review_id": review_id, "status": "MANUAL_REVIEW_REQUIRED", "snapshot_id": snapshot_id}

    event_result = _availability_event(db, row)
    outbound_id = event_result.get("outbound_event_id") if event_result else None
    db.execute(
        """
        UPDATE availability_reward_reviews
        SET status = 'AUTO_APPROVED', auto_review_result = 'CLEAN',
            outbound_event_id = ?, approved_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (outbound_id, now, now, review_id),
    )
    db.commit()
    review = _review_row(db, review_id)
    snapshot_id = create_reward_snapshot(db, row, review, {"stage": "AUTO_APPROVED"})
    return {"review_id": review_id, "status": "AUTO_APPROVED", "event": event_result, "snapshot_id": snapshot_id}


@availability_bp.post("/heartbeat")
def heartbeat():
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = _payload()
    role = data.get("actor_role") or session.get("role")
    actor_id = data.get("actor_external_id") or session.get("target_code") or session.get("user_id")
    client_id = data.get("client_session_id") or "default"
    device_fingerprint = data.get("device_fingerprint") or request.headers.get("X-Device-Fingerprint") or ""
    ip_hash = _hash(request.headers.get("X-Forwarded-For") or request.remote_addr or "")
    user_agent_hash = _hash(request.headers.get("User-Agent") or "")

    try:
        db = get_db()
        row, created = ping(db, role, actor_id, client_id)
        db.execute(
            """
            UPDATE availability_sessions
            SET device_fingerprint = COALESCE(NULLIF(?, ''), device_fingerprint),
                ip_hash = COALESCE(NULLIF(?, ''), ip_hash),
                user_agent_hash = COALESCE(NULLIF(?, ''), user_agent_hash)
            WHERE id = ?
            """,
            (device_fingerprint, ip_hash, user_agent_hash, row["id"]),
        )
        db.commit()
        row = row_by_id(db, row["id"])
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

    state, minutes, points, note = _close_state(db, row)
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
    review_result = _review_and_event(db, row)
    return jsonify({"ok": True, "state": state, "session": as_dict(row), "reward_review": review_result})

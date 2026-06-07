import json
from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_dict(row):
    return dict(row) if row else {}


def _outbound_event(db, outbound_event_id):
    if not outbound_event_id:
        return {}
    row = db.execute(
        "SELECT * FROM timeblock_outbound_events WHERE id = ? LIMIT 1",
        (outbound_event_id,),
    ).fetchone()
    return _as_dict(row)


def create_reward_snapshot(db, session_row, review_row=None, extra=None):
    session_data = _as_dict(session_row)
    review_data = _as_dict(review_row)
    extra = extra or {}

    session_id = session_data.get("id")
    review_id = review_data.get("id")
    outbound_event_id = review_data.get("outbound_event_id") or extra.get("outbound_event_id")
    outbound_data = _outbound_event(db, outbound_event_id)

    retry_state = {
        "outbound_event_id": outbound_event_id,
        "outbound_status": outbound_data.get("status") or "",
        "retry_count": int(outbound_data.get("retry_count") or 0),
        "next_retry_at": outbound_data.get("next_retry_at") or "",
        "last_attempt_at": outbound_data.get("last_attempt_at") or "",
        "last_error": outbound_data.get("last_error") or outbound_data.get("error_message") or "",
        "dead_letter_status": "DEAD_LETTER" if outbound_data.get("status") == "DEAD_LETTER" else "",
    }

    snapshot = {
        "source_project": "fumapgo",
        "session": session_data,
        "review": review_data,
        "outbound_event": outbound_data,
        "retry_state": retry_state,
        "extra": extra,
        "created_at": now_iso(),
    }

    cur = db.execute(
        """
        INSERT INTO reward_audit_snapshots (
            source_project, session_id, review_id, actor_role, actor_external_id,
            device_fingerprint, ip_hash, user_agent_hash, eligible_minutes,
            reward_points, review_reason, auto_review_result, outbound_event_id,
            snapshot_json, created_at
        )
        VALUES ('fumapgo', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            review_id,
            session_data.get("actor_role"),
            session_data.get("actor_external_id"),
            session_data.get("device_fingerprint"),
            session_data.get("ip_hash"),
            session_data.get("user_agent_hash"),
            int(session_data.get("eligible_minutes") or 0),
            int(session_data.get("reward_points") or 0),
            session_data.get("review_reason") or review_data.get("review_reason") or "",
            review_data.get("auto_review_result") or extra.get("auto_review_result") or "",
            outbound_event_id,
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            snapshot["created_at"],
        ),
    )
    db.commit()
    return cur.lastrowid

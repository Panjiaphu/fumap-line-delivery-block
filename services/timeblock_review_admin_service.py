from datetime import datetime, timezone

from services.reward_audit_snapshot_service import create_reward_snapshot
from services.timeblock_gateway_service import forward_event_to_timeblock, get_outbound_event, record_outbound_event


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _as_dict(row):
    return dict(row) if row else {}


def _review(db, review_id):
    return db.execute(
        "SELECT * FROM availability_reward_reviews WHERE id = ? LIMIT 1",
        (review_id,),
    ).fetchone()


def _session(db, session_id):
    return db.execute(
        "SELECT * FROM availability_sessions WHERE id = ? LIMIT 1",
        (session_id,),
    ).fetchone()


def pending_reviews(db, limit=50):
    rows = db.execute(
        """
        SELECT *
        FROM availability_reward_reviews
        WHERE status = 'MANUAL_REVIEW_REQUIRED'
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (max(1, min(int(limit or 50), 100)),),
    ).fetchall()
    return [_as_dict(row) for row in rows]


def review_metrics(db):
    rows = db.execute(
        """
        SELECT status, COUNT(*) AS c
        FROM availability_reward_reviews
        GROUP BY status
        """
    ).fetchall()
    counts = {row["status"].lower(): int(row["c"] or 0) for row in rows}
    return {
        "pending": counts.get("manual_review_required", 0),
        "approved": counts.get("approved", 0),
        "rejected": counts.get("rejected", 0),
        "auto_approved": counts.get("auto_approved", 0),
        "raw": counts,
    }


def _availability_event(db, session_row):
    code = "STORE_ONLINE_REWARD_FINALIZED" if session_row["actor_role"] == "STORE" else "SHIPPER_ONLINE_REWARD_FINALIZED"
    event = {
        "source_project": "fumapgo",
        "event_code": code,
        "external_event_id": f"availability:{session_row['id']}",
        "idempotency_key": f"fumapgo:availability:{session_row['id']}",
        "actor_role": session_row["actor_role"],
        "actor_external_id": session_row["actor_external_id"],
        "points_delta": int(session_row["reward_points"] or 0),
        "occurred_at": session_row["ended_at"] or now_iso(),
        "payload": {
            "session_id": session_row["id"],
            "session_key": session_row["session_key"],
            "eligible_minutes": int(session_row["eligible_minutes"] or 0),
            "contract_version": "v1",
        },
    }
    out, created = record_outbound_event(db, event)
    result = forward_event_to_timeblock(db, out) if created else {"duplicate": True}
    out = get_outbound_event(db, out["id"])
    return {"created": created, "forward_result": result, "outbound_event_id": out["id"], "status": out["status"]}


def approve_review(db, review_id):
    review = _review(db, review_id)
    if not review:
        return {"ok": False, "error": "review not found"}
    if review["status"] not in {"MANUAL_REVIEW_REQUIRED", "PENDING_REWARD"}:
        return {"ok": False, "error": "review is not approvable", "status": review["status"]}
    session = _session(db, review["session_id"])
    if not session:
        return {"ok": False, "error": "session not found"}
    if int(session["reward_points"] or 0) <= 0:
        return {"ok": False, "error": "session has no reward_points"}

    event_result = _availability_event(db, session)
    outbound_id = event_result.get("outbound_event_id") if event_result else None
    now = now_iso()
    db.execute(
        """
        UPDATE availability_reward_reviews
        SET status = 'APPROVED', outbound_event_id = ?, approved_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (outbound_id, now, now, review["id"]),
    )
    db.commit()
    review = _review(db, review["id"])
    snapshot_id = create_reward_snapshot(db, session, review, {"stage": "ADMIN_APPROVED"})
    return {"ok": True, "review_id": review["id"], "status": "APPROVED", "event": event_result, "snapshot_id": snapshot_id}


def reject_review(db, review_id, reason=""):
    review = _review(db, review_id)
    if not review:
        return {"ok": False, "error": "review not found"}
    if review["status"] not in {"MANUAL_REVIEW_REQUIRED", "PENDING_REWARD"}:
        return {"ok": False, "error": "review is not rejectable", "status": review["status"]}
    session = _session(db, review["session_id"])
    now = now_iso()
    note = reason or review["review_reason"] or "admin_rejected"
    db.execute(
        """
        UPDATE availability_reward_reviews
        SET status = 'REJECTED', review_reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (note, now, review["id"]),
    )
    db.commit()
    review = _review(db, review["id"])
    snapshot_id = create_reward_snapshot(db, session, review, {"stage": "ADMIN_REJECTED", "reason": note}) if session else None
    return {"ok": True, "review_id": review["id"], "status": "REJECTED", "snapshot_id": snapshot_id}

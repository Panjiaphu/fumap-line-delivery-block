from services.timeblock_gateway_service import forward_event_to_timeblock, get_outbound_event, now_iso


def list_dead_letters(db, limit=50):
    rows = db.execute(
        """
        SELECT *
        FROM timeblock_outbound_events
        WHERE status = 'DEAD_LETTER'
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (max(1, min(int(limit or 50), 100)),),
    ).fetchall()
    return [dict(row) for row in rows]


def reset_dead_letter_for_retry(db, event_id):
    now = now_iso()
    row = get_outbound_event(db, event_id)
    if not row:
        return {"ok": False, "error": "event not found"}
    if row["status"] != "DEAD_LETTER":
        return {"ok": False, "error": "event is not DEAD_LETTER", "status": row["status"]}

    db.execute(
        """
        UPDATE timeblock_outbound_events
        SET status = 'RETRY_PENDING', next_retry_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (now, now, row["id"]),
    )
    db.commit()
    row = get_outbound_event(db, row["id"])
    return {"ok": True, "event_id": row["id"], "status": row["status"]}


def retry_dead_letter_now(db, event_id):
    reset = reset_dead_letter_for_retry(db, event_id)
    if not reset.get("ok"):
        return reset
    row = get_outbound_event(db, event_id)
    result = forward_event_to_timeblock(db, row)
    current = get_outbound_event(db, event_id)
    return {
        "ok": True,
        "event_id": event_id,
        "status": current["status"] if current else "UNKNOWN",
        "result": result,
    }

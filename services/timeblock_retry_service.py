from datetime import datetime, timezone

from services.timeblock_gateway_service import forward_event_to_timeblock


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def retry_pending_events(db, limit=20):
    now = now_iso()
    rows = db.execute(
        """
        SELECT *
        FROM timeblock_outbound_events
        WHERE status = 'RETRY_PENDING'
          AND COALESCE(next_retry_at, '') <= ?
        ORDER BY next_retry_at ASC, id ASC
        LIMIT ?
        """,
        (now, int(limit or 20)),
    ).fetchall()

    results = []
    for row in rows:
        result = forward_event_to_timeblock(db, row)
        results.append({
            "id": row["id"],
            "idempotency_key": row["idempotency_key"],
            "previous_status": row["status"],
            "result": result,
        })

    return {
        "ok": True,
        "processed": len(results),
        "results": results,
    }

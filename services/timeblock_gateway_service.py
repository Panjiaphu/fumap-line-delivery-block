import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from services.block_service import create_block
from services.timeblock_contract_service import apply_contract_version


SOURCE_PROJECT = "fumapgo"
MAX_RETRY_COUNT = 5
RETRY_DELAYS_MINUTES = [5, 15, 60, 240]

ALLOWED_EVENT_CODES = {
    "CUSTOMER_ORDER_CREATED",
    "CUSTOMER_ORDER_COMPLETED",
    "CUSTOMER_BONUS_GRANTED",
    "STORE_ONLINE_REWARD_FINALIZED",
    "SHIPPER_ONLINE_REWARD_FINALIZED",
    "ADMIN_REWARD_REVIEWED",
}

ALLOWED_ACTOR_ROLES = {
    "CUSTOMER",
    "STORE",
    "DRIVER",
    "SHIPPER",
    "ADMIN_OPERATOR",
}


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(data):
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _env(key, default=""):
    return (os.getenv(key, default) or "").strip()


def timeblock_config():
    return {
        "source_project": SOURCE_PROJECT,
        "project_code": _env("TIMEBLOCK_PROJECT_CODE", SOURCE_PROJECT) or SOURCE_PROJECT,
        "webapp_url": _env("TIMEBLOCK_WEBAPP_URL", "https://fumap-bot-life.onrender.com").rstrip("/"),
        "api_base_url": _env("TIMEBLOCK_API_BASE_URL", "").rstrip("/"),
        "project_token_configured": bool(_env("TIMEBLOCK_PROJECT_TOKEN", "")),
    }


def ensure_timeblock_gateway_schema(app=None):
    """
    Local integration queue / audit table for FUMAP GO -> Timeblock.

    This is not a wallet ledger and not the Timeblock source of truth.
    It records outbound event attempts, validates idempotency, and keeps an
    auditable local trail before/after forwarding to Timeblock.
    """
    if app is None:
        from flask import current_app
        app = current_app

    import sqlite3
    from pathlib import Path

    db_path = app.config["DATABASE_PATH"]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS timeblock_outbound_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_project TEXT NOT NULL DEFAULT 'fumapgo',
                event_code TEXT NOT NULL,
                external_event_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                actor_external_id TEXT NOT NULL,
                points_delta INTEGER DEFAULT 0,
                occurred_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'QUEUED',
                payload_json TEXT,
                response_json TEXT,
                error_message TEXT,
                retry_count INTEGER DEFAULT 0,
                next_retry_at TEXT,
                last_attempt_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                forwarded_at TEXT,
                UNIQUE(idempotency_key)
            );

            CREATE INDEX IF NOT EXISTS idx_timeblock_outbound_source_project
            ON timeblock_outbound_events(source_project);

            CREATE INDEX IF NOT EXISTS idx_timeblock_outbound_event_code
            ON timeblock_outbound_events(event_code);

            CREATE INDEX IF NOT EXISTS idx_timeblock_outbound_actor
            ON timeblock_outbound_events(actor_role, actor_external_id);

            CREATE INDEX IF NOT EXISTS idx_timeblock_outbound_status
            ON timeblock_outbound_events(status);

            CREATE INDEX IF NOT EXISTS idx_timeblock_outbound_next_retry
            ON timeblock_outbound_events(status, next_retry_at);
            """
        )

        for column_sql in [
            "ALTER TABLE timeblock_outbound_events ADD COLUMN retry_count INTEGER DEFAULT 0",
            "ALTER TABLE timeblock_outbound_events ADD COLUMN next_retry_at TEXT",
            "ALTER TABLE timeblock_outbound_events ADD COLUMN last_attempt_at TEXT",
            "ALTER TABLE timeblock_outbound_events ADD COLUMN last_error TEXT",
        ]:
            try:
                conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass

        conn.commit()
    finally:
        conn.close()


def _normalize_role(role):
    role = (role or "").strip().upper()
    if role == "SHIPPER":
        return "DRIVER"
    return role


def validate_event_payload(data):
    data = dict(data or {})

    source_project = (data.get("source_project") or SOURCE_PROJECT).strip().lower()
    event_code = (data.get("event_code") or "").strip().upper()
    external_event_id = (data.get("external_event_id") or "").strip()
    idempotency_key = (data.get("idempotency_key") or "").strip()
    actor_role = _normalize_role(data.get("actor_role") or "")
    actor_external_id = (data.get("actor_external_id") or "").strip()
    occurred_at = (data.get("occurred_at") or now_iso()).strip()

    errors = []

    if source_project != SOURCE_PROJECT:
        errors.append("source_project must be fumapgo")

    if event_code not in ALLOWED_EVENT_CODES:
        errors.append("event_code is not allowed")

    if not external_event_id:
        errors.append("external_event_id is required")

    if not idempotency_key:
        errors.append("idempotency_key is required")

    if actor_role not in ALLOWED_ACTOR_ROLES:
        errors.append("actor_role is not allowed")

    if not actor_external_id:
        errors.append("actor_external_id is required")

    try:
        points_delta = int(data.get("points_delta") or 0)
    except Exception:
        points_delta = 0
        errors.append("points_delta must be an integer")

    if points_delta < 0:
        errors.append("points_delta cannot be negative from FUMAP GO gateway")

    normalized = apply_contract_version({
        "source_project": SOURCE_PROJECT,
        "event_code": event_code,
        "external_event_id": external_event_id,
        "idempotency_key": idempotency_key,
        "actor_role": actor_role,
        "actor_external_id": actor_external_id,
        "points_delta": points_delta,
        "occurred_at": occurred_at,
        "payload": data.get("payload") or {},
    })

    return len(errors) == 0, errors, normalized


def record_outbound_event(db, event):
    event = apply_contract_version(event)
    existing = db.execute(
        """
        SELECT *
        FROM timeblock_outbound_events
        WHERE idempotency_key = ?
        LIMIT 1
        """,
        (event["idempotency_key"],),
    ).fetchone()

    if existing:
        return existing, False

    created_at = now_iso()
    payload_json = canonical_json(event.get("payload") or {})

    cur = db.execute(
        """
        INSERT INTO timeblock_outbound_events (
            source_project,
            event_code,
            external_event_id,
            idempotency_key,
            actor_role,
            actor_external_id,
            points_delta,
            occurred_at,
            status,
            payload_json,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?)
        """,
        (
            SOURCE_PROJECT,
            event["event_code"],
            event["external_event_id"],
            event["idempotency_key"],
            event["actor_role"],
            event["actor_external_id"],
            int(event.get("points_delta") or 0),
            event["occurred_at"],
            payload_json,
            created_at,
            created_at,
        ),
    )

    create_block(
        db,
        event_type="TIMEBLOCK_EVENT_QUEUED",
        actor_role=event["actor_role"],
        actor_code=event["actor_external_id"],
        new_status="QUEUED",
        payload={
            "source_project": SOURCE_PROJECT,
            "event_code": event["event_code"],
            "external_event_id": event["external_event_id"],
            "idempotency_key": event["idempotency_key"],
            "points_delta": int(event.get("points_delta") or 0),
            "contract_version": event.get("contract_version", "v1"),
        },
        commit=False,
    )

    db.commit()

    return db.execute(
        """
        SELECT *
        FROM timeblock_outbound_events
        WHERE id = ?
        """,
        (cur.lastrowid,),
    ).fetchone(), True


def _retry_policy(row):
    next_count = int(row["retry_count"] or 0) + 1
    if next_count >= MAX_RETRY_COUNT:
        return "DEAD_LETTER", next_count, ""

    idx = min(next_count - 1, len(RETRY_DELAYS_MINUTES) - 1)
    delay_minutes = RETRY_DELAYS_MINUTES[idx]
    next_retry_at = (datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)).replace(microsecond=0).isoformat()
    return "RETRY_PENDING", next_count, next_retry_at


def _update_event_status(db, event_id, *, status, response=None, error=""):
    now = now_iso()
    row = db.execute(
        "SELECT * FROM timeblock_outbound_events WHERE id = ? LIMIT 1",
        (event_id,),
    ).fetchone()

    retry_count = int(row["retry_count"] or 0) if row else 0
    next_retry_at = row["next_retry_at"] if row else None
    last_error = error or ""
    last_attempt_at = now

    if status == "FORWARDED":
        retry_count = 0
        next_retry_at = None
        last_error = ""
    elif status == "FAILED":
        status, retry_count, next_retry_at = _retry_policy(row)

    db.execute(
        """
        UPDATE timeblock_outbound_events
        SET status = ?,
            response_json = ?,
            error_message = ?,
            retry_count = ?,
            next_retry_at = ?,
            last_attempt_at = ?,
            last_error = ?,
            updated_at = ?,
            forwarded_at = CASE WHEN ? = 'FORWARDED' THEN ? ELSE forwarded_at END
        WHERE id = ?
        """,
        (
            status,
            canonical_json(response or {}),
            error or "",
            retry_count,
            next_retry_at,
            last_attempt_at,
            last_error,
            now,
            status,
            now,
            event_id,
        ),
    )

    create_block(
        db,
        event_type="TIMEBLOCK_EVENT_FORWARD_STATUS",
        actor_role="SYSTEM",
        actor_code="FUMAPGO_GATEWAY",
        new_status=status,
        payload={
            "timeblock_outbound_event_id": event_id,
            "status": status,
            "retry_count": retry_count,
            "next_retry_at": next_retry_at,
            "error": error or "",
        },
        commit=False,
    )

    db.commit()


def forward_event_to_timeblock(db, row):
    cfg = timeblock_config()
    token = _env("TIMEBLOCK_PROJECT_TOKEN", "")

    if not cfg["api_base_url"] or not token:
        _update_event_status(
            db,
            row["id"],
            status="QUEUED",
            response={"skipped": True},
            error="TIMEBLOCK_API_BASE_URL or TIMEBLOCK_PROJECT_TOKEN is not configured",
        )
        return {"ok": False, "queued": True, "forwarded": False, "error": "timeblock config missing"}

    payload = apply_contract_version({
        "source_project": row["source_project"],
        "event_code": row["event_code"],
        "external_event_id": row["external_event_id"],
        "idempotency_key": row["idempotency_key"],
        "actor_role": row["actor_role"],
        "actor_external_id": row["actor_external_id"],
        "points_delta": int(row["points_delta"] or 0),
        "occurred_at": row["occurred_at"],
        "payload": json.loads(row["payload_json"] or "{}"),
    })

    url = f"{cfg['api_base_url']}/api/projects/{cfg['project_code']}/events"
    body = canonical_json(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": row["idempotency_key"],
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
            try:
                response_json = json.loads(raw or "{}")
            except Exception:
                response_json = {"raw": raw}

        _update_event_status(db, row["id"], status="FORWARDED", response=response_json)
        return {"ok": True, "queued": True, "forwarded": True, "response": response_json, "status": "FORWARDED"}

    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        _update_event_status(db, row["id"], status="FAILED", error=f"HTTP {exc.code}: {error_body}")
        current = get_outbound_event(db, row["id"])
        return {"ok": False, "queued": True, "forwarded": False, "status": current["status"], "error": f"HTTP {exc.code}: {error_body}"}

    except Exception as exc:
        _update_event_status(db, row["id"], status="FAILED", error=str(exc))
        current = get_outbound_event(db, row["id"])
        return {"ok": False, "queued": True, "forwarded": False, "status": current["status"], "error": str(exc)}


def get_outbound_event(db, event_id):
    return db.execute(
        """
        SELECT *
        FROM timeblock_outbound_events
        WHERE id = ?
        """,
        (event_id,),
    ).fetchone()


def integration_health(db):
    cfg = timeblock_config()
    status_counts = {}

    try:
        rows = db.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM timeblock_outbound_events
            GROUP BY status
            """
        ).fetchall()
        status_counts = {row["status"]: int(row["c"] or 0) for row in rows}
    except Exception:
        status_counts = {}

    return {
        "ok": True,
        "source_project": SOURCE_PROJECT,
        "project_code": cfg["project_code"],
        "webapp_url": cfg["webapp_url"],
        "api_base_url_configured": bool(cfg["api_base_url"]),
        "project_token_configured": cfg["project_token_configured"],
        "event_ingestion_ready": bool(cfg["api_base_url"] and cfg["project_token_configured"]),
        "status_counts": status_counts,
    }


def wallet_summary_url(actor_external_id):
    cfg = timeblock_config()
    if not cfg["api_base_url"]:
        return ""
    return f"{cfg['api_base_url']}/api/projects/{cfg['project_code']}/wallets/{actor_external_id}/summary"


def wallet_blocks_url(actor_external_id):
    cfg = timeblock_config()
    if not cfg["api_base_url"]:
        return ""
    return f"{cfg['api_base_url']}/api/projects/{cfg['project_code']}/wallets/{actor_external_id}/blocks"

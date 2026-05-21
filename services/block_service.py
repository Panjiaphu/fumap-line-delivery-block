import json
import hashlib

from services.code_service import now_iso, unique_code, generate_block_code


def canonical_json(data) -> str:
    return json.dumps(data or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_payload(data) -> str:
    raw = canonical_json(data)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_block(
    db,
    *,
    event_type,
    actor_role,
    actor_id=None,
    actor_code="",
    order_id=None,
    order_code="",
    previous_status="",
    new_status="",
    amount_twd=0,
    payload=None,
    commit=True,
):
    now = now_iso()
    payload = payload or {}

    block_code = unique_code(db, "blocks", "block_code", generate_block_code)
    payload_json = canonical_json(payload)
    payload_hash = hash_payload(
        {
            "event_type": event_type,
            "actor_role": actor_role,
            "actor_id": actor_id,
            "actor_code": actor_code,
            "order_id": order_id,
            "order_code": order_code,
            "previous_status": previous_status,
            "new_status": new_status,
            "amount_twd": amount_twd,
            "payload": payload,
            "created_at": now,
        }
    )

    cur = db.execute(
        """
        INSERT INTO blocks (
            block_code,
            event_type,
            actor_role,
            actor_id,
            actor_code,
            order_id,
            order_code,
            previous_status,
            new_status,
            amount_twd,
            payload_json,
            payload_hash,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            block_code,
            event_type,
            actor_role,
            actor_id,
            actor_code,
            order_id,
            order_code,
            previous_status,
            new_status,
            int(amount_twd or 0),
            payload_json,
            payload_hash,
            now,
        ),
    )

    if commit:
        db.commit()

    return db.execute(
        """
        SELECT *
        FROM blocks
        WHERE id = ?
        """,
        (cur.lastrowid,),
    ).fetchone()


def get_order_blocks(db, order_code):
    return db.execute(
        """
        SELECT *
        FROM blocks
        WHERE order_code = ?
        ORDER BY id ASC
        """,
        (order_code,),
    ).fetchall()


def get_recent_blocks(db, limit=100):
    return db.execute(
        """
        SELECT *
        FROM blocks
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit or 100),),
    ).fetchall()


def create_auth_block(db, *, event_type, user, payload=None, commit=True):
    return create_block(
        db,
        event_type=event_type,
        actor_role=user["role"],
        actor_id=user["id"],
        actor_code=user["login_id"],
        payload=payload or {},
        commit=commit,
    )

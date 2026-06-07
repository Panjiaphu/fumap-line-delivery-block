from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def norm_role(role):
    role = (role or '').strip().upper()
    return 'DRIVER' if role == 'SHIPPER' else role


def session_key(role, actor_id, client_id='default'):
    return f"fumapgo:{norm_role(role)}:{str(actor_id).strip()}:{(client_id or 'default').strip()}"


def row_by_key(db, key):
    return db.execute(
        'SELECT * FROM availability_sessions WHERE session_key = ? LIMIT 1',
        (key,),
    ).fetchone()


def row_by_id(db, session_id):
    return db.execute(
        'SELECT * FROM availability_sessions WHERE id = ? LIMIT 1',
        (int(session_id or 0),),
    ).fetchone()


def ping(db, role, actor_id, client_id='default'):
    role = norm_role(role)
    actor_id = str(actor_id or '').strip()
    if role not in {'STORE', 'DRIVER'}:
        raise ValueError('role must be STORE or DRIVER')
    if not actor_id:
        raise ValueError('actor_id is required')

    key = session_key(role, actor_id, client_id)
    now = now_iso()
    row = row_by_key(db, key)
    if not row:
        db.execute(
            """
            INSERT INTO availability_sessions
            (source_project, actor_role, actor_external_id, session_key, status,
             started_at, last_seen_at, ping_count, created_at, updated_at)
            VALUES ('fumapgo', ?, ?, ?, 'ACTIVE', ?, ?, 1, ?, ?)
            """,
            (role, actor_id, key, now, now, now, now),
        )
        db.commit()
        return row_by_key(db, key), True
    if row['status'] != 'ACTIVE':
        raise ValueError('session is not active')
    db.execute(
        """
        UPDATE availability_sessions
        SET last_seen_at = ?, ping_count = COALESCE(ping_count, 0) + 1, updated_at = ?
        WHERE id = ?
        """,
        (now, now, row['id']),
    )
    db.commit()
    return row_by_key(db, key), False


def as_dict(row):
    if not row:
        return None
    return {k: row[k] for k in row.keys()}

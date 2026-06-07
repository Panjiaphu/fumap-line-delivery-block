from datetime import datetime, timezone


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_availability_session_schema(app=None):
    if app is None:
        from flask import current_app
        app = current_app

    import sqlite3
    from pathlib import Path

    db_path = app.config["DATABASE_PATH"]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS availability_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_project TEXT NOT NULL DEFAULT 'fumapgo',
            actor_role TEXT NOT NULL,
            actor_external_id TEXT NOT NULL,
            session_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            ended_at TEXT,
            ping_count INTEGER DEFAULT 0,
            eligible_minutes INTEGER DEFAULT 0,
            reward_points INTEGER DEFAULT 0,
            review_reason TEXT,
            device_fingerprint TEXT,
            ip_hash TEXT,
            user_agent_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        );
        """)

        for column_sql in [
            "ALTER TABLE availability_sessions ADD COLUMN device_fingerprint TEXT",
            "ALTER TABLE availability_sessions ADD COLUMN ip_hash TEXT",
            "ALTER TABLE availability_sessions ADD COLUMN user_agent_hash TEXT",
        ]:
            try:
                conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass

        conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_availability_device
        ON availability_sessions(actor_role, actor_external_id, device_fingerprint);
        """)
        conn.commit()
    finally:
        conn.close()

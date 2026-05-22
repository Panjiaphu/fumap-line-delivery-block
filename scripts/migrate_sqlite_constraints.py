#!/usr/bin/env python3
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def database_path():
    return Path(
        os.getenv(
            "DATABASE_PATH",
            os.getenv("SQLITE_PATH", str(BASE_DIR / "fumap_go.sqlite3")),
        )
    ).resolve()


def connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def now_stamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def table_exists(conn, table_name):
    row = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return bool(row)


def table_sql(conn, table_name):
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()

    return row["sql"] if row and row["sql"] else ""


def table_columns(conn, table_name):
    if not table_exists(conn, table_name):
        return []

    return [
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    ]


def row_count(conn, table_name):
    if not table_exists(conn, table_name):
        return 0

    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table_name}").fetchone()
    return int(row["c"] or 0) if row else 0


def create_backup(db_path):
    if not db_path.exists():
        print(f"[SKIP] database not found: {db_path}")
        return None

    backup_path = db_path.with_name(f"{db_path.name}.bak.{now_stamp()}")
    shutil.copy2(db_path, backup_path)
    print(f"[OK] backup created: {backup_path}")
    return backup_path


def drop_indexes_for_table(conn, table_name):
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'index'
          AND tbl_name = ?
          AND sql IS NOT NULL
        """,
        (table_name,),
    ).fetchall()

    for row in rows:
        index_name = row["name"]
        conn.execute(f'DROP INDEX IF EXISTS "{index_name}"')


def copy_common_columns(conn, old_table, new_table):
    old_cols = table_columns(conn, old_table)
    new_cols = table_columns(conn, new_table)

    common_cols = [col for col in old_cols if col in new_cols]

    if not common_cols:
        print(f"[WARN] no common columns between {old_table} and {new_table}")
        return 0

    col_sql = ", ".join([f'"{col}"' for col in common_cols])

    conn.execute(
        f"""
        INSERT INTO "{new_table}" ({col_sql})
        SELECT {col_sql}
        FROM "{old_table}"
        """
    )

    return row_count(conn, new_table)


def migrate_line_contact_bindings(conn):
    table_name = "line_contact_bindings"

    if not table_exists(conn, table_name):
        print(f"[SKIP] {table_name} does not exist")
        return

    current_sql = table_sql(conn, table_name)

    if "'ADMIN'" in current_sql and "CHECK(role IN" in current_sql:
        print(f"[SKIP] {table_name} already allows ADMIN")
        return

    old_table = f"{table_name}_old_{now_stamp()}"

    print(f"[MIGRATE] {table_name}")
    before_count = row_count(conn, table_name)

    drop_indexes_for_table(conn, table_name)

    conn.execute(f'ALTER TABLE "{table_name}" RENAME TO "{old_table}"')

    conn.execute(
        """
        CREATE TABLE line_contact_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL CHECK(role IN ('CUSTOMER','STORE','DRIVER','ADMIN')),
            target_code TEXT NOT NULL,
            contact_code TEXT UNIQUE NOT NULL,
            line_user_id TEXT NOT NULL,
            line_display_name TEXT,
            status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','DISABLED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    copied_count = copy_common_columns(conn, old_table, table_name)

    conn.execute(f'DROP TABLE "{old_table}"')

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_line_bindings_role_target
        ON line_contact_bindings(role, target_code)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_line_bindings_line_user_id
        ON line_contact_bindings(line_user_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_line_bindings_contact_code
        ON line_contact_bindings(contact_code)
        """
    )

    print(f"[OK] {table_name}: rows before={before_count}, copied={copied_count}")


def migrate_accounting_entries(conn):
    table_name = "accounting_entries"

    if not table_exists(conn, table_name):
        print(f"[SKIP] {table_name} does not exist")
        return

    current_sql = table_sql(conn, table_name)

    if all(value in current_sql for value in ["'INFO'", "'CREDIT'", "'DEBIT'"]):
        print(f"[SKIP] {table_name} already allows INFO/CREDIT/DEBIT")
        return

    old_table = f"{table_name}_old_{now_stamp()}"

    print(f"[MIGRATE] {table_name}")
    before_count = row_count(conn, table_name)

    drop_indexes_for_table(conn, table_name)

    conn.execute(f'ALTER TABLE "{table_name}" RENAME TO "{old_table}"')

    conn.execute(
        """
        CREATE TABLE accounting_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_code TEXT UNIQUE NOT NULL,

            order_id INTEGER,
            order_code TEXT,

            entry_type TEXT NOT NULL,
            role TEXT NOT NULL,
            target_code TEXT,

            amount_twd INTEGER NOT NULL DEFAULT 0,
            direction TEXT NOT NULL CHECK(direction IN ('INFO','CREDIT','DEBIT','IN','OUT')),
            note TEXT,

            created_at TEXT NOT NULL,

            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
        """
    )

    copied_count = copy_common_columns(conn, old_table, table_name)

    conn.execute(f'DROP TABLE "{old_table}"')

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_accounting_entries_order_code
        ON accounting_entries(order_code)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_accounting_entries_role_target
        ON accounting_entries(role, target_code)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_accounting_entries_created_at
        ON accounting_entries(created_at)
        """
    )

    print(f"[OK] {table_name}: rows before={before_count}, copied={copied_count}")


def main():
    db_path = database_path()

    print("[START] SQLite CHECK constraint migration")
    print(f"[INFO] DATABASE_PATH={db_path}")

    if not db_path.exists():
        raise SystemExit(f"[ERROR] database file not found: {db_path}")

    create_backup(db_path)

    conn = connect(db_path)

    try:
        conn.execute("BEGIN")

        migrate_line_contact_bindings(conn)
        migrate_accounting_entries(conn)

        conn.commit()
        print("[OK] migration committed")

    except Exception as exc:
        conn.rollback()
        print(f"[ERROR] migration rolled back: {exc}")
        raise

    finally:
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass

        conn.close()

    print("[DONE] SQLite CHECK constraint migration completed")


if __name__ == "__main__":
    main()

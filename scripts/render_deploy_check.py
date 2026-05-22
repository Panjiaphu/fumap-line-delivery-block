#!/usr/bin/env python3
import os
import sqlite3
import subprocess
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent


def run(cmd, cwd=None):
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd or BASE_DIR),
            capture_output=True,
            text=True,
            check=False,
        )

        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()

        if result.returncode == 0:
            return out or "-"

        return f"[ERROR exit={result.returncode}] {err or out or '-'}"

    except Exception as exc:
        return f"[ERROR] {exc}"


def env_value(key, default=""):
    return str(os.getenv(key, default) or "").strip()


def env_set(key):
    return bool(env_value(key))


def yesno(value):
    return "YES" if bool(value) else "NO"


def database_path():
    return Path(
        env_value(
            "DATABASE_PATH",
            env_value("SQLITE_PATH", str(BASE_DIR / "fumap_go.sqlite3")),
        )
    ).resolve()


def file_line_count(path):
    path = Path(path)

    if not path.exists():
        return 0

    try:
        return len(path.read_text(encoding="utf-8").splitlines())
    except Exception:
        return 0


def file_contains(path, text):
    path = Path(path)

    if not path.exists():
        return False

    try:
        return text in path.read_text(encoding="utf-8")
    except Exception:
        return False


def connect_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


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


def table_count(conn, table_name):
    if not table_exists(conn, table_name):
        return 0

    row = conn.execute(f"SELECT COUNT(*) AS c FROM {table_name}").fetchone()
    return int(row["c"] or 0) if row else 0


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


def check_db():
    db_path = database_path()

    print("")
    print("== DATABASE ==")
    print(f"DATABASE_PATH: {db_path}")
    print(f"Exists: {yesno(db_path.exists())}")

    if not db_path.exists():
        print("[WARN] Database file not found. Check Render DATABASE_PATH env.")
        return

    conn = connect_db(db_path)

    try:
        for table in [
            "users",
            "stores",
            "drivers",
            "orders",
            "line_contact_bindings",
            "accounting_entries",
            "blocks",
        ]:
            table_is_present = table_exists(conn, table)
            table_rows = table_count(conn, table)

            print(
                f"{table}: "
                f"exists={yesno(table_is_present)}, "
                f"rows={table_rows}"
            )

        line_sql = table_sql(conn, "line_contact_bindings")
        acc_sql = table_sql(conn, "accounting_entries")

        line_allows_admin = "'ADMIN'" in line_sql
        acc_allows_info = "'INFO'" in acc_sql
        acc_allows_credit = "'CREDIT'" in acc_sql
        acc_allows_debit = "'DEBIT'" in acc_sql

        print("")
        print("== DB CONSTRAINT CHECK ==")
        print(f"line_contact_bindings allows ADMIN: {yesno(line_allows_admin)}")
        print(f"accounting_entries allows INFO: {yesno(acc_allows_info)}")
        print(f"accounting_entries allows CREDIT: {yesno(acc_allows_credit)}")
        print(f"accounting_entries allows DEBIT: {yesno(acc_allows_debit)}")

    finally:
        conn.close()


def check_files():
    admin_routes = BASE_DIR / "routes" / "admin_routes.py"
    accounting_service = BASE_DIR / "services" / "accounting_service.py"
    line_routes = BASE_DIR / "routes" / "line_routes.py"
    bind_html = BASE_DIR / "templates" / "mobile" / "line" / "bind.html"
    app_py = BASE_DIR / "app.py"
    schema_sql = BASE_DIR / "schema.sql"

    app_has_render_commit = file_contains(app_py, "RENDER_GIT_COMMIT")
    app_has_health_render = file_contains(app_py, "/health/render")
    accounting_has_admin_summary = file_contains(
        accounting_service,
        "def admin_accounting_summary",
    )
    accounting_has_list_entries = file_contains(
        accounting_service,
        "def list_admin_accounting_entries",
    )
    admin_has_safe_query = file_contains(admin_routes, "_safe_query_all")
    line_has_liff_debug = file_contains(line_routes, "line_liff_id_set")
    bind_has_credentials = file_contains(bind_html, 'credentials: "same-origin"')
    schema_allows_admin = file_contains(
        schema_sql,
        "'CUSTOMER','STORE','DRIVER','ADMIN'",
    )
    schema_allows_accounting_directions = file_contains(
        schema_sql,
        "'INFO','CREDIT','DEBIT','IN','OUT'",
    )

    print("")
    print("== FILE CHECK ==")
    print(f"app.py lines: {file_line_count(app_py)}")
    print(f"routes/admin_routes.py lines: {file_line_count(admin_routes)}")
    print(f"services/accounting_service.py lines: {file_line_count(accounting_service)}")
    print(f"routes/line_routes.py lines: {file_line_count(line_routes)}")

    print("")
    print("== PATCH PRESENCE CHECK ==")
    print(f"app.py has RENDER_GIT_COMMIT: {yesno(app_has_render_commit)}")
    print(f"app.py has /health/render: {yesno(app_has_health_render)}")
    print(
        "accounting_service has admin_accounting_summary: "
        f"{yesno(accounting_has_admin_summary)}"
    )
    print(
        "accounting_service has list_admin_accounting_entries: "
        f"{yesno(accounting_has_list_entries)}"
    )
    print(f"admin_routes has safe accounting query: {yesno(admin_has_safe_query)}")
    print(f"line_routes has line_liff_id_set: {yesno(line_has_liff_debug)}")
    print(f"bind.html has credentials same-origin: {yesno(bind_has_credentials)}")
    print(f"schema allows ADMIN role: {yesno(schema_allows_admin)}")
    print(
        "schema allows INFO/CREDIT/DEBIT: "
        f"{yesno(schema_allows_accounting_directions)}"
    )


def check_git():
    render_service = env_value(
        "RENDER_SERVICE_NAME",
        env_value("RENDER_SERVICE_ID", "-"),
    )
    render_commit = env_value("RENDER_GIT_COMMIT", "-")
    current_branch = run(["git", "branch", "--show-current"])
    current_head = run(["git", "rev-parse", "HEAD"])
    latest_commit = run(["git", "log", "-1", "--oneline"])
    git_status = run(["git", "status", "--short"])

    print("== GIT / RENDER ==")
    print(f"Render service: {render_service}")
    print(f"Render git commit env: {render_commit}")
    print(f"Current branch: {current_branch}")
    print(f"Current HEAD: {current_head}")
    print(f"Latest commit: {latest_commit}")
    print(f"Git status: {git_status}")


def check_env():
    app_env = env_value("APP_ENV", env_value("FLASK_ENV", "-"))

    print("")
    print("== ENV SAFE CHECK ==")
    print(f"APP_ENV / FLASK_ENV: {app_env}")
    print(f"PUBLIC_BASE_URL: {env_value('PUBLIC_BASE_URL', '-')}")
    print(f"APP_BASE_URL: {env_value('APP_BASE_URL', '-')}")
    print(f"DATABASE_PATH set: {yesno(env_set('DATABASE_PATH'))}")
    print(f"LINE_LIFF_ID set: {yesno(env_set('LINE_LIFF_ID'))}")
    print(f"LINEHOOK_BASE_URL set: {yesno(env_set('LINEHOOK_BASE_URL'))}")
    print(f"LINE_GATEWAY_BASE_URL set: {yesno(env_set('LINE_GATEWAY_BASE_URL'))}")
    print(f"FGO_INTERNAL_SECRET set: {yesno(env_set('FGO_INTERNAL_SECRET'))}")
    print(f"FGO_ADMIN_LINE_USER_ID set: {yesno(env_set('FGO_ADMIN_LINE_USER_ID'))}")
    print(f"SESSION_COOKIE_SECURE: {env_value('SESSION_COOKIE_SECURE', '-')}")
    print(f"SESSION_COOKIE_SAMESITE: {env_value('SESSION_COOKIE_SAMESITE', '-')}")


def main():
    print("[START] Render deploy check")
    print(f"BASE_DIR: {BASE_DIR}")

    check_git()
    check_env()
    check_files()
    check_db()

    print("")
    print("[DONE] Render deploy check completed")


if __name__ == "__main__":
    main()

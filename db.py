import sqlite3
from pathlib import Path

from flask import current_app, g


SQLITE_TIMEOUT_SECONDS = 5
SQLITE_BUSY_TIMEOUT_MS = 5000


def _configure_sqlite_connection(conn):
    """
    SQLite stability settings for Render + persistent disk.

    WAL helps concurrent reads while writes are happening.
    busy_timeout reduces "database is locked" failures under short write bursts.
    synchronous=NORMAL is a practical MVP balance for WAL mode.
    """
    pragmas = [
        "PRAGMA foreign_keys = ON",
        "PRAGMA journal_mode = WAL",
        f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}",
        "PRAGMA synchronous = NORMAL",
        "PRAGMA temp_store = MEMORY",
    ]

    for pragma in pragmas:
        try:
            conn.execute(pragma)
        except sqlite3.OperationalError:
            continue

    return conn


def get_db():
    if "db" not in g:
        db_path = current_app.config["DATABASE_PATH"]
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            db_path,
            timeout=SQLITE_TIMEOUT_SECONDS,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row

        _configure_sqlite_connection(conn)

        g.db = conn

    return g.db


def close_db(error=None):
    conn = g.pop("db", None)

    if conn is not None:
        conn.close()


def init_db(app=None):
    """
    Safe init for FUMAP GO Commercial V1.

    Supports existing SQLite database by:
    1. Creating minimal tables.
    2. Adding missing columns.
    3. Running schema.sql if available.
    4. Re-checking columns and indexes.
    """
    if app is None:
        app = current_app

    db_path = app.config["DATABASE_PATH"]
    schema_path = Path(app.root_path) / "schema.sql"

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_TIMEOUT_SECONDS,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row

    _configure_sqlite_connection(conn)

    _create_minimal_tables(conn)
    _ensure_mvp_columns(conn)

    if schema_path.exists():
        with schema_path.open("r", encoding="utf-8") as f:
            schema_sql = f.read()

        try:
            conn.executescript(schema_sql)
        except sqlite3.OperationalError as e:
            message = str(e)

            if "no such column" in message:
                _ensure_mvp_columns(conn)
                conn.executescript(schema_sql)
            else:
                raise

    _ensure_mvp_columns(conn)
    _ensure_indexes(conn)
    conn.commit()
    conn.close()


def query_one(sql, params=None):
    params = params or []
    return get_db().execute(sql, params).fetchone()


def query_all(sql, params=None):
    params = params or []
    return get_db().execute(sql, params).fetchall()


def execute(sql, params=None, commit=True):
    params = params or []
    conn = get_db()
    cur = conn.execute(sql, params)

    if commit:
        conn.commit()

    return cur


def table_columns(conn, table_name):
    try:
        return {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
    except Exception:
        return set()


def add_column_if_missing(conn, table_name, column_name, column_sql):
    cols = table_columns(conn, table_name)

    if column_name not in cols:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def _create_minimal_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS drivers (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS line_contact_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS line_push_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS accounting_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS settlement_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS settlement_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS system_flags (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );

        CREATE TABLE IF NOT EXISTS email_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );
        """
    )


def _ensure_mvp_columns(conn):
    safe_columns = {
        "users": [
            ("login_id", "login_id TEXT"),
            ("password_hash", "password_hash TEXT"),
            ("role", "role TEXT DEFAULT 'CUSTOMER'"),
            ("display_name", "display_name TEXT"),
            ("phone", "phone TEXT"),
            ("email", "email TEXT"),
            ("email_verified_at", "email_verified_at TEXT"),
            ("email_verification_token", "email_verification_token TEXT"),
            ("email_verification_expires_at", "email_verification_expires_at TEXT"),
            ("email_verification_sent_at", "email_verification_sent_at TEXT"),
            ("password_reset_token", "password_reset_token TEXT"),
            ("password_reset_expires_at", "password_reset_expires_at TEXT"),
            ("password_reset_used_at", "password_reset_used_at TEXT"),
            ("status", "status TEXT DEFAULT 'ACTIVE'"),
            ("created_at", "created_at TEXT"),
            ("updated_at", "updated_at TEXT"),
        ],
        "stores": [
            ("store_code", "store_code TEXT"),
            ("owner_user_id", "owner_user_id INTEGER"),
            ("store_name", "store_name TEXT"),
            ("phone", "phone TEXT"),
            ("address", "address TEXT"),
            ("store_lat", "store_lat REAL DEFAULT 0"),
            ("store_lng", "store_lng REAL DEFAULT 0"),
            ("category", "category TEXT"),
            ("description", "description TEXT"),
            ("banner_url", "banner_url TEXT"),
            ("city_block", "city_block TEXT DEFAULT 'ZHONGLI'"),
            ("area_label", "area_label TEXT DEFAULT '中壢區'"),
            ("is_open", "is_open INTEGER DEFAULT 1"),
            ("open_time", "open_time TEXT DEFAULT '10:00'"),
            ("close_time", "close_time TEXT DEFAULT '21:00'"),
            ("open_days_json", "open_days_json TEXT"),
            (
                "last_order_minutes_before_close",
                "last_order_minutes_before_close INTEGER DEFAULT 30",
            ),
            ("is_temporarily_closed", "is_temporarily_closed INTEGER DEFAULT 0"),
            ("temporary_close_reason", "temporary_close_reason TEXT"),
            ("status_reason", "status_reason TEXT"),
            ("approved_at", "approved_at TEXT"),
            ("approved_by_admin_id", "approved_by_admin_id INTEGER DEFAULT 0"),
            ("setup_completed", "setup_completed INTEGER DEFAULT 0"),
            ("contract_signed_at", "contract_signed_at TEXT"),
            ("contract_payload_json", "contract_payload_json TEXT"),
            ("contract_hash", "contract_hash TEXT"),

            # Admin Settlement V1 payout account fields.
            ("payout_bank_name", "payout_bank_name TEXT"),
            ("payout_bank_code", "payout_bank_code TEXT"),
            ("payout_bank_account", "payout_bank_account TEXT"),
            ("payout_account_name", "payout_account_name TEXT"),
            ("payout_note", "payout_note TEXT"),

            ("status", "status TEXT DEFAULT 'ACTIVE'"),
            ("created_at", "created_at TEXT"),
            ("updated_at", "updated_at TEXT"),
        ],
        "drivers": [
            ("driver_code", "driver_code TEXT"),
            ("user_id", "user_id INTEGER"),
            ("driver_name", "driver_name TEXT"),
            ("phone", "phone TEXT"),
            ("service_area", "service_area TEXT"),
            ("city_block", "city_block TEXT DEFAULT 'ZHONGLI'"),
            ("area_label", "area_label TEXT DEFAULT '中壢區'"),
            ("vehicle_type", "vehicle_type TEXT"),
            ("is_online", "is_online INTEGER DEFAULT 0"),
            ("smartroad_lane", "smartroad_lane TEXT"),
            ("status_reason", "status_reason TEXT"),
            ("approved_at", "approved_at TEXT"),
            ("approved_by_admin_id", "approved_by_admin_id INTEGER DEFAULT 0"),
            ("contract_signed_at", "contract_signed_at TEXT"),
            ("contract_payload_json", "contract_payload_json TEXT"),
            ("contract_hash", "contract_hash TEXT"),

            # Admin Settlement V1 payout account fields.
            ("payout_bank_name", "payout_bank_name TEXT"),
            ("payout_bank_code", "payout_bank_code TEXT"),
            ("payout_bank_account", "payout_bank_account TEXT"),
            ("payout_account_name", "payout_account_name TEXT"),
            ("payout_note", "payout_note TEXT"),

            ("status", "status TEXT DEFAULT 'ACTIVE'"),
            ("created_at", "created_at TEXT"),
            ("updated_at", "updated_at TEXT"),
        ],
        "products": [
            ("store_id", "store_id INTEGER"),
            ("name", "name TEXT"),
            ("price_twd", "price_twd INTEGER DEFAULT 0"),
            ("description", "description TEXT"),
            ("image_url", "image_url TEXT"),
            ("product_category", "product_category TEXT DEFAULT '主餐'"),
            ("sort_order", "sort_order INTEGER DEFAULT 0"),
            ("stock_qty", "stock_qty INTEGER DEFAULT 999"),
            ("prepare_minutes", "prepare_minutes INTEGER DEFAULT 15"),
            ("product_note", "product_note TEXT"),
            ("is_active", "is_active INTEGER DEFAULT 1"),
            ("created_at", "created_at TEXT"),
            ("updated_at", "updated_at TEXT"),
        ],
        "orders": [
            ("order_code", "order_code TEXT"),
            ("customer_user_id", "customer_user_id INTEGER"),
            ("store_id", "store_id INTEGER"),
            ("driver_id", "driver_id INTEGER"),
            ("status", "status TEXT DEFAULT 'CREATED'"),
            ("payment_method", "payment_method TEXT DEFAULT 'COD'"),
            ("payment_status", "payment_status TEXT DEFAULT 'UNPAID'"),
            ("delivery_method", "delivery_method TEXT DEFAULT 'FACE_TO_FACE'"),
            ("subtotal_twd", "subtotal_twd INTEGER DEFAULT 0"),
            ("delivery_fee_twd", "delivery_fee_twd INTEGER DEFAULT 0"),
            ("base_delivery_fee_twd", "base_delivery_fee_twd INTEGER DEFAULT 0"),
            (
                "customer_delivery_share_twd",
                "customer_delivery_share_twd INTEGER DEFAULT 0",
            ),
            (
                "store_delivery_support_twd",
                "store_delivery_support_twd INTEGER DEFAULT 0",
            ),
            ("delivery_fee_rule_json", "delivery_fee_rule_json TEXT"),
            ("service_fee_twd", "service_fee_twd INTEGER DEFAULT 0"),
            ("extra_fee_twd", "extra_fee_twd INTEGER DEFAULT 0"),
            ("rain_fee_twd", "rain_fee_twd INTEGER DEFAULT 0"),
            ("total_twd", "total_twd INTEGER DEFAULT 0"),
            ("delivery_address", "delivery_address TEXT"),
            ("delivery_lat", "delivery_lat REAL DEFAULT 0"),
            ("delivery_lng", "delivery_lng REAL DEFAULT 0"),
            ("distance_band", "distance_band TEXT DEFAULT '0-2KM'"),
            ("floor_number", "floor_number TEXT"),
            ("address_note", "address_note TEXT"),
            ("extra_fee_reason", "extra_fee_reason TEXT"),
            ("difficulty_flags_json", "difficulty_flags_json TEXT"),
            ("customer_name", "customer_name TEXT"),
            ("customer_phone", "customer_phone TEXT"),
            ("note", "note TEXT"),
            ("proof_image_url", "proof_image_url TEXT"),

            # Customer Invoice / Receipt Request Note V1.
            # This is not FUMAP GO issuing a tax invoice.
            # It only stores customer request data so the store can handle receipt/invoice manually if supported.
            ("invoice_required", "invoice_required INTEGER DEFAULT 0"),
            ("invoice_type", "invoice_type TEXT DEFAULT 'NONE'"),
            ("invoice_title", "invoice_title TEXT"),
            ("invoice_tax_id", "invoice_tax_id TEXT"),
            ("invoice_note", "invoice_note TEXT"),

            # Payment proof/review fields.
            ("payment_proof_image_url", "payment_proof_image_url TEXT"),
            ("payment_proof_uploaded_at", "payment_proof_uploaded_at TEXT"),
            ("payment_proof_status", "payment_proof_status TEXT DEFAULT 'PENDING_REVIEW'"),
            ("payment_proof_reviewed_at", "payment_proof_reviewed_at TEXT"),
            ("payment_proof_reviewed_by_admin_id", "payment_proof_reviewed_by_admin_id INTEGER"),

            # Payment V1C explicit approval/rejection fields.
            ("payment_verified_at", "payment_verified_at TEXT"),
            ("payment_verified_by", "payment_verified_by INTEGER"),
            ("payment_rejected_at", "payment_rejected_at TEXT"),
            ("payment_reject_reason", "payment_reject_reason TEXT"),

            # Delivery proof email fields.
            ("delivery_proof_image_url", "delivery_proof_image_url TEXT"),
            ("delivery_proof_uploaded_at", "delivery_proof_uploaded_at TEXT"),
            ("delivery_proof_sent_email_at", "delivery_proof_sent_email_at TEXT"),

            # Return-to-store proof fields.
            ("return_proof_image_url", "return_proof_image_url TEXT"),
            ("return_proof_uploaded_at", "return_proof_uploaded_at TEXT"),

            ("city_block", "city_block TEXT DEFAULT 'ZHONGLI'"),
            ("area_label", "area_label TEXT DEFAULT '中壢區'"),
            ("smartroad_lane", "smartroad_lane TEXT"),
            ("distance_km", "distance_km REAL DEFAULT 0"),
            ("smartroad_score", "smartroad_score INTEGER DEFAULT 50"),
            ("smartroad_score_label", "smartroad_score_label TEXT DEFAULT 'UNKNOWN'"),
            ("smartroad_reasons_json", "smartroad_reasons_json TEXT"),
            ("smartroad_same_road", "smartroad_same_road INTEGER DEFAULT 0"),
            ("smartroad_same_side", "smartroad_same_side INTEGER DEFAULT 0"),
            ("smartroad_uturn_risk", "smartroad_uturn_risk INTEGER DEFAULT 0"),
            ("store_road_name", "store_road_name TEXT"),
            ("customer_road_name", "customer_road_name TEXT"),
            ("store_house_number", "store_house_number TEXT"),
            ("customer_house_number", "customer_house_number TEXT"),
            ("store_house_parity", "store_house_parity TEXT"),
            ("customer_house_parity", "customer_house_parity TEXT"),
            ("admin_hold", "admin_hold INTEGER DEFAULT 0"),
            ("admin_hold_reason", "admin_hold_reason TEXT"),
            ("admin_hold_at", "admin_hold_at TEXT"),
            ("order_source", "order_source TEXT DEFAULT 'CUSTOMER_MARKETPLACE'"),
            ("store_created_by", "store_created_by INTEGER"),
            ("manual_order_title", "manual_order_title TEXT"),
            ("prepaid_to", "prepaid_to TEXT"),
            ("created_at", "created_at TEXT"),
            ("updated_at", "updated_at TEXT"),
        ],
        "order_items": [
            ("order_id", "order_id INTEGER"),
            ("product_id", "product_id INTEGER"),
            ("product_name", "product_name TEXT"),
            ("unit_price_twd", "unit_price_twd INTEGER DEFAULT 0"),
            ("qty", "qty INTEGER DEFAULT 1"),
            ("line_total_twd", "line_total_twd INTEGER DEFAULT 0"),
            ("created_at", "created_at TEXT"),
        ],
        "line_contact_bindings": [
            ("role", "role TEXT"),
            ("target_code", "target_code TEXT"),
            ("contact_code", "contact_code TEXT"),
            ("line_user_id", "line_user_id TEXT"),
            ("line_display_name", "line_display_name TEXT"),
            ("status", "status TEXT DEFAULT 'ACTIVE'"),
            ("created_at", "created_at TEXT"),
            ("updated_at", "updated_at TEXT"),
        ],
        "line_push_logs": [
            ("contact_code", "contact_code TEXT"),
            ("line_user_id", "line_user_id TEXT"),
            ("event_type", "event_type TEXT"),
            ("target_role", "target_role TEXT"),
            ("target_code", "target_code TEXT"),
            ("order_code", "order_code TEXT"),
            ("message_preview", "message_preview TEXT"),
            ("push_status", "push_status TEXT"),
            ("gateway_response", "gateway_response TEXT"),
            ("created_at", "created_at TEXT"),
        ],
        "blocks": [
            ("block_code", "block_code TEXT"),
            ("event_type", "event_type TEXT"),
            ("actor_role", "actor_role TEXT"),
            ("actor_id", "actor_id INTEGER"),
            ("actor_code", "actor_code TEXT"),
            ("order_id", "order_id INTEGER"),
            ("order_code", "order_code TEXT"),
            ("previous_status", "previous_status TEXT"),
            ("new_status", "new_status TEXT"),
            ("amount_twd", "amount_twd INTEGER DEFAULT 0"),
            ("payload_json", "payload_json TEXT"),
            ("payload_hash", "payload_hash TEXT"),
            ("created_at", "created_at TEXT"),
        ],
        "accounting_entries": [
            ("entry_code", "entry_code TEXT"),
            ("order_id", "order_id INTEGER"),
            ("order_code", "order_code TEXT"),
            ("entry_type", "entry_type TEXT"),
            ("role", "role TEXT"),
            ("target_code", "target_code TEXT"),
            ("amount_twd", "amount_twd INTEGER DEFAULT 0"),
            ("direction", "direction TEXT"),
            ("note", "note TEXT"),
            ("created_at", "created_at TEXT"),
        ],
        "settlement_confirmations": [
            ("confirmation_code", "confirmation_code TEXT"),
            ("order_id", "order_id INTEGER"),
            ("order_code", "order_code TEXT"),
            ("confirmation_type", "confirmation_type TEXT"),
            ("payer_role", "payer_role TEXT"),
            ("payer_code", "payer_code TEXT"),
            ("receiver_role", "receiver_role TEXT"),
            ("receiver_code", "receiver_code TEXT"),
            ("amount_twd", "amount_twd INTEGER DEFAULT 0"),
            ("status", "status TEXT DEFAULT 'CONFIRMED'"),
            ("note", "note TEXT"),
            ("admin_user_id", "admin_user_id INTEGER DEFAULT 0"),
            ("admin_login_id", "admin_login_id TEXT"),
            ("created_at", "created_at TEXT"),
            ("updated_at", "updated_at TEXT"),
        ],
        "settlement_batches": [
            ("settlement_code", "settlement_code TEXT"),
            ("role", "role TEXT"),
            ("target_code", "target_code TEXT"),
            ("target_user_id", "target_user_id INTEGER"),
            ("target_email", "target_email TEXT"),
            ("direction", "direction TEXT"),
            ("settlement_type", "settlement_type TEXT"),
            ("period_start", "period_start TEXT"),
            ("period_end", "period_end TEXT"),
            ("amount_twd", "amount_twd INTEGER DEFAULT 0"),
            ("status", "status TEXT DEFAULT 'DRAFT'"),
            ("email_sent_at", "email_sent_at TEXT"),
            ("paid_confirmed_at", "paid_confirmed_at TEXT"),
            ("paid_confirmed_by", "paid_confirmed_by INTEGER"),
            ("payment_method", "payment_method TEXT DEFAULT 'BANK_TRANSFER'"),
            ("admin_bank_snapshot_json", "admin_bank_snapshot_json TEXT"),
            ("target_payout_snapshot_json", "target_payout_snapshot_json TEXT"),
            ("related_order_codes_json", "related_order_codes_json TEXT"),
            ("note", "note TEXT"),
            ("created_at", "created_at TEXT"),
            ("updated_at", "updated_at TEXT"),
        ],
        "system_flags": [
            ("flag_key", "flag_key TEXT"),
            ("flag_value", "flag_value TEXT DEFAULT '0'"),
            ("updated_at", "updated_at TEXT"),
        ],
        "email_logs": [
            ("event_type", "event_type TEXT"),
            ("recipient_email", "recipient_email TEXT"),
            ("recipient_role", "recipient_role TEXT"),
            ("user_id", "user_id INTEGER"),
            ("order_id", "order_id INTEGER"),
            ("order_code", "order_code TEXT"),
            ("subject", "subject TEXT"),
            ("status", "status TEXT"),
            ("error_message", "error_message TEXT"),
            ("provider_message_id", "provider_message_id TEXT"),
            ("retry_count", "retry_count INTEGER DEFAULT 0"),
            ("last_attempt_at", "last_attempt_at TEXT"),
            ("created_at", "created_at TEXT"),
        ],
    }

    for table, columns in safe_columns.items():
        for column_name, column_sql in columns:
            add_column_if_missing(conn, table, column_name, column_sql)

    _backfill_defaults(conn)

def _backfill_defaults(conn):
    updates = [
        """
        UPDATE users
        SET role = COALESCE(NULLIF(role, ''), 'CUSTOMER'),
            status = COALESCE(NULLIF(status, ''), 'ACTIVE'),
            created_at = COALESCE(NULLIF(created_at, ''), datetime('now', '+8 hours')),
            updated_at = COALESCE(NULLIF(updated_at, ''), datetime('now', '+8 hours'))
        """,
        """
        UPDATE stores
        SET store_name = COALESCE(NULLIF(store_name, ''), '未命名店家'),
            store_lat = COALESCE(store_lat, 0),
            store_lng = COALESCE(store_lng, 0),
            city_block = COALESCE(NULLIF(city_block, ''), 'ZHONGLI'),
            area_label = COALESCE(NULLIF(area_label, ''), '中壢區'),
            is_open = COALESCE(is_open, 1),
            open_time = COALESCE(NULLIF(open_time, ''), '10:00'),
            close_time = COALESCE(NULLIF(close_time, ''), '21:00'),
            last_order_minutes_before_close = COALESCE(last_order_minutes_before_close, 30),
            is_temporarily_closed = COALESCE(is_temporarily_closed, 0),
            approved_by_admin_id = COALESCE(approved_by_admin_id, 0),
            setup_completed = COALESCE(setup_completed, 0),
            payout_bank_name = COALESCE(payout_bank_name, ''),
            payout_bank_code = COALESCE(payout_bank_code, ''),
            payout_bank_account = COALESCE(payout_bank_account, ''),
            payout_account_name = COALESCE(payout_account_name, ''),
            payout_note = COALESCE(payout_note, ''),
            status = COALESCE(NULLIF(status, ''), 'ACTIVE'),
            created_at = COALESCE(NULLIF(created_at, ''), datetime('now', '+8 hours')),
            updated_at = COALESCE(NULLIF(updated_at, ''), datetime('now', '+8 hours'))
        """,
        """
        UPDATE drivers
        SET driver_name = COALESCE(NULLIF(driver_name, ''), '未命名 Shiper'),
            city_block = COALESCE(NULLIF(city_block, ''), 'ZHONGLI'),
            area_label = COALESCE(NULLIF(area_label, ''), '中壢區'),
            is_online = COALESCE(is_online, 0),
            approved_by_admin_id = COALESCE(approved_by_admin_id, 0),
            payout_bank_name = COALESCE(payout_bank_name, ''),
            payout_bank_code = COALESCE(payout_bank_code, ''),
            payout_bank_account = COALESCE(payout_bank_account, ''),
            payout_account_name = COALESCE(payout_account_name, ''),
            payout_note = COALESCE(payout_note, ''),
            status = COALESCE(NULLIF(status, ''), 'ACTIVE'),
            created_at = COALESCE(NULLIF(created_at, ''), datetime('now', '+8 hours')),
            updated_at = COALESCE(NULLIF(updated_at, ''), datetime('now', '+8 hours'))
        """,
        """
        UPDATE products
        SET price_twd = COALESCE(price_twd, 0),
            product_category = COALESCE(NULLIF(product_category, ''), '主餐'),
            sort_order = COALESCE(sort_order, 0),
            stock_qty = COALESCE(stock_qty, 999),
            prepare_minutes = COALESCE(prepare_minutes, 15),
            is_active = COALESCE(is_active, 1),
            created_at = COALESCE(NULLIF(created_at, ''), datetime('now', '+8 hours')),
            updated_at = COALESCE(NULLIF(updated_at, ''), datetime('now', '+8 hours'))
        """,
        """
        UPDATE orders
        SET status = COALESCE(NULLIF(status, ''), 'CREATED'),
            payment_method = COALESCE(NULLIF(payment_method, ''), 'COD'),
            payment_status = COALESCE(NULLIF(payment_status, ''), 'UNPAID'),
            delivery_method = COALESCE(NULLIF(delivery_method, ''), 'FACE_TO_FACE'),
            subtotal_twd = COALESCE(subtotal_twd, 0),
            delivery_fee_twd = COALESCE(delivery_fee_twd, 0),
            base_delivery_fee_twd = COALESCE(base_delivery_fee_twd, delivery_fee_twd, 0),
            customer_delivery_share_twd = COALESCE(customer_delivery_share_twd, delivery_fee_twd, 0),
            store_delivery_support_twd = COALESCE(store_delivery_support_twd, 0),
            service_fee_twd = COALESCE(service_fee_twd, 0),
            extra_fee_twd = COALESCE(extra_fee_twd, 0),
            rain_fee_twd = COALESCE(rain_fee_twd, 0),
            total_twd = COALESCE(total_twd, 0),
            delivery_lat = COALESCE(delivery_lat, 0),
            delivery_lng = COALESCE(delivery_lng, 0),
            distance_band = COALESCE(NULLIF(distance_band, ''), '0-2KM'),
            invoice_required = COALESCE(invoice_required, 0),
            invoice_type = COALESCE(NULLIF(invoice_type, ''), 'NONE'),
            invoice_title = COALESCE(invoice_title, ''),
            invoice_tax_id = COALESCE(invoice_tax_id, ''),
            invoice_note = COALESCE(invoice_note, ''),
            city_block = COALESCE(NULLIF(city_block, ''), 'ZHONGLI'),
            area_label = COALESCE(NULLIF(area_label, ''), '中壢區'),
            distance_km = COALESCE(distance_km, 0),
            smartroad_score = COALESCE(smartroad_score, 50),
            smartroad_score_label = COALESCE(NULLIF(smartroad_score_label, ''), 'UNKNOWN'),
            smartroad_same_road = COALESCE(smartroad_same_road, 0),
            smartroad_same_side = COALESCE(smartroad_same_side, 0),
            smartroad_uturn_risk = COALESCE(smartroad_uturn_risk, 0),
            admin_hold = COALESCE(admin_hold, 0),
            payment_proof_status = COALESCE(NULLIF(payment_proof_status, ''), 'PENDING_REVIEW'),
            order_source = COALESCE(NULLIF(order_source, ''), 'CUSTOMER_MARKETPLACE'),
            created_at = COALESCE(NULLIF(created_at, ''), datetime('now', '+8 hours')),
            updated_at = COALESCE(NULLIF(updated_at, ''), datetime('now', '+8 hours'))
        """,
        """
        UPDATE order_items
        SET qty = COALESCE(qty, 1),
            unit_price_twd = COALESCE(unit_price_twd, 0),
            line_total_twd = COALESCE(line_total_twd, 0),
            created_at = COALESCE(NULLIF(created_at, ''), datetime('now', '+8 hours'))
        """,
        """
        UPDATE line_contact_bindings
        SET status = COALESCE(NULLIF(status, ''), 'ACTIVE'),
            created_at = COALESCE(NULLIF(created_at, ''), datetime('now', '+8 hours')),
            updated_at = COALESCE(NULLIF(updated_at, ''), datetime('now', '+8 hours'))
        """,
        """
        UPDATE settlement_confirmations
        SET amount_twd = COALESCE(amount_twd, 0),
            status = COALESCE(NULLIF(status, ''), 'CONFIRMED'),
            admin_user_id = COALESCE(admin_user_id, 0),
            created_at = COALESCE(NULLIF(created_at, ''), datetime('now', '+8 hours')),
            updated_at = COALESCE(NULLIF(updated_at, ''), datetime('now', '+8 hours'))
        """,
        """
        UPDATE settlement_batches
        SET amount_twd = COALESCE(amount_twd, 0),
            status = COALESCE(NULLIF(status, ''), 'DRAFT'),
            payment_method = COALESCE(NULLIF(payment_method, ''), 'BANK_TRANSFER'),
            created_at = COALESCE(NULLIF(created_at, ''), datetime('now', '+8 hours')),
            updated_at = COALESCE(NULLIF(updated_at, ''), datetime('now', '+8 hours'))
        """,
        """
        UPDATE email_logs
        SET retry_count = COALESCE(retry_count, 0),
            created_at = COALESCE(NULLIF(created_at, ''), datetime('now', '+8 hours'))
        """,
    ]

    for sql in updates:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            continue


def _ensure_indexes(conn):
    """
    Indexes for core system lookup and realtime boards.
    """
    index_sql = [
        "CREATE INDEX IF NOT EXISTS idx_users_login_id ON users(login_id)",
        "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)",
        "CREATE INDEX IF NOT EXISTS idx_users_email_verification_token ON users(email_verification_token)",
        "CREATE INDEX IF NOT EXISTS idx_users_password_reset_token ON users(password_reset_token)",
        "CREATE INDEX IF NOT EXISTS idx_users_role_status ON users(role, status)",

        "CREATE INDEX IF NOT EXISTS idx_stores_store_code ON stores(store_code)",
        "CREATE INDEX IF NOT EXISTS idx_stores_owner_user_id ON stores(owner_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_stores_status ON stores(status)",
        "CREATE INDEX IF NOT EXISTS idx_stores_city_block ON stores(city_block)",
        "CREATE INDEX IF NOT EXISTS idx_stores_open_status ON stores(is_open, is_temporarily_closed)",
        "CREATE INDEX IF NOT EXISTS idx_stores_business_hours ON stores(open_time, close_time)",
        "CREATE INDEX IF NOT EXISTS idx_stores_status_city ON stores(status, city_block)",

        "CREATE INDEX IF NOT EXISTS idx_drivers_driver_code ON drivers(driver_code)",
        "CREATE INDEX IF NOT EXISTS idx_drivers_user_id ON drivers(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_drivers_status ON drivers(status)",
        "CREATE INDEX IF NOT EXISTS idx_drivers_city_block ON drivers(city_block)",
        "CREATE INDEX IF NOT EXISTS idx_drivers_online_city_status ON drivers(is_online, city_block, status)",

        "CREATE INDEX IF NOT EXISTS idx_products_store_id ON products(store_id)",
        "CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active)",
        "CREATE INDEX IF NOT EXISTS idx_products_store_active_sort ON products(store_id, is_active, sort_order, id)",

        "CREATE INDEX IF NOT EXISTS idx_orders_order_code ON orders(order_code)",
        "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)",
        "CREATE INDEX IF NOT EXISTS idx_orders_admin_hold ON orders(admin_hold)",
        "CREATE INDEX IF NOT EXISTS idx_orders_city_block ON orders(city_block)",
        "CREATE INDEX IF NOT EXISTS idx_orders_order_source ON orders(order_source)",
        "CREATE INDEX IF NOT EXISTS idx_orders_store_created_by ON orders(store_created_by)",
        "CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_updated_at ON orders(updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_payment_method ON orders(payment_method)",
        "CREATE INDEX IF NOT EXISTS idx_orders_payment_status ON orders(payment_status)",
        "CREATE INDEX IF NOT EXISTS idx_orders_invoice_required ON orders(invoice_required)",
        "CREATE INDEX IF NOT EXISTS idx_orders_invoice_type ON orders(invoice_type)",

        "CREATE INDEX IF NOT EXISTS idx_orders_payment_proof_status ON orders(payment_proof_status)",
        "CREATE INDEX IF NOT EXISTS idx_orders_payment_proof_uploaded_at ON orders(payment_proof_uploaded_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_payment_verified_at ON orders(payment_verified_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_payment_verified_by ON orders(payment_verified_by)",
        "CREATE INDEX IF NOT EXISTS idx_orders_payment_rejected_at ON orders(payment_rejected_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_payment_pending_review ON orders(payment_method, payment_status, admin_hold, payment_proof_status)",

        "CREATE INDEX IF NOT EXISTS idx_orders_delivery_proof_uploaded_at ON orders(delivery_proof_uploaded_at)",

        "CREATE INDEX IF NOT EXISTS idx_orders_store_status_hold_id ON orders(store_id, status, admin_hold, id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_store_status_updated ON orders(store_id, status, updated_at)",
        "CREATE INDEX IF NOT EXISTS idx_orders_store_hold_status_updated ON orders(store_id, admin_hold, status, updated_at)",

        "CREATE INDEX IF NOT EXISTS idx_orders_waiting_city_hold_driver_id ON orders(status, city_block, admin_hold, driver_id, id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_waiting_driver_null_city ON orders(status, driver_id, city_block, admin_hold, id)",

        "CREATE INDEX IF NOT EXISTS idx_orders_driver_status_id ON orders(driver_id, status, id)",
        "CREATE INDEX IF NOT EXISTS idx_orders_driver_status_updated ON orders(driver_id, status, updated_at)",

        "CREATE INDEX IF NOT EXISTS idx_orders_distance_band ON orders(distance_band)",
        "CREATE INDEX IF NOT EXISTS idx_orders_distance_km ON orders(distance_km)",
        "CREATE INDEX IF NOT EXISTS idx_orders_smartroad_score ON orders(smartroad_score)",
        "CREATE INDEX IF NOT EXISTS idx_orders_smartroad_same_road ON orders(smartroad_same_road)",
        "CREATE INDEX IF NOT EXISTS idx_orders_smartroad_uturn_risk ON orders(smartroad_uturn_risk)",
        "CREATE INDEX IF NOT EXISTS idx_orders_city_status_smartroad ON orders(city_block, status, smartroad_score)",

        "CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id)",
        "CREATE INDEX IF NOT EXISTS idx_order_items_product_id ON order_items(product_id)",

        "CREATE INDEX IF NOT EXISTS idx_line_bindings_role_target ON line_contact_bindings(role, target_code)",
        "CREATE INDEX IF NOT EXISTS idx_line_bindings_contact_code ON line_contact_bindings(contact_code)",
        "CREATE INDEX IF NOT EXISTS idx_line_bindings_line_user_id ON line_contact_bindings(line_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_line_push_logs_order_code ON line_push_logs(order_code)",
        "CREATE INDEX IF NOT EXISTS idx_line_push_logs_target ON line_push_logs(target_role, target_code)",
        "CREATE INDEX IF NOT EXISTS idx_line_push_logs_created_at ON line_push_logs(created_at)",

        "CREATE INDEX IF NOT EXISTS idx_blocks_order_code_id ON blocks(order_code, id)",
        "CREATE INDEX IF NOT EXISTS idx_blocks_event_type ON blocks(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_blocks_actor ON blocks(actor_role, actor_code)",
        "CREATE INDEX IF NOT EXISTS idx_blocks_created_at ON blocks(created_at)",

        "CREATE INDEX IF NOT EXISTS idx_accounting_order_code_id ON accounting_entries(order_code, id)",
        "CREATE INDEX IF NOT EXISTS idx_accounting_target ON accounting_entries(role, target_code)",
        "CREATE INDEX IF NOT EXISTS idx_accounting_created_at ON accounting_entries(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_accounting_entry_type ON accounting_entries(entry_type)",

        "CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_order_code ON settlement_confirmations(order_code)",
        "CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_type ON settlement_confirmations(confirmation_type)",
        "CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_payer ON settlement_confirmations(payer_role, payer_code)",
        "CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_receiver ON settlement_confirmations(receiver_role, receiver_code)",
        "CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_created_at ON settlement_confirmations(created_at)",

        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_code ON settlement_batches(settlement_code)",
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_role_target ON settlement_batches(role, target_code)",
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_direction_type ON settlement_batches(direction, settlement_type)",
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_status ON settlement_batches(status)",
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_period ON settlement_batches(period_start, period_end)",
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_created_at ON settlement_batches(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_settlement_batches_target_email ON settlement_batches(target_email)",

        "CREATE INDEX IF NOT EXISTS idx_system_flags_key ON system_flags(flag_key)",

        "CREATE INDEX IF NOT EXISTS idx_email_logs_order_code ON email_logs(order_code)",
        "CREATE INDEX IF NOT EXISTS idx_email_logs_recipient ON email_logs(recipient_email)",
        "CREATE INDEX IF NOT EXISTS idx_email_logs_event_type ON email_logs(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_email_logs_created_at ON email_logs(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_email_logs_status ON email_logs(status)",
    ]

    for sql in index_sql:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            continue
            

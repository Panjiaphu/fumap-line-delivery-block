PRAGMA foreign_keys = ON;

-- =========================================================
-- FUMAP GO Commercial / Phase 6 Lite Schema
-- Safe for SQLite + Render persistent disk
-- Rule:
-- - db.py remains the runtime migration source of truth.
-- - schema.sql should not use restrictive CHECK constraints that block new statuses.
-- =========================================================


-- =========================================================
-- users
-- =========================================================
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    login_id TEXT UNIQUE,
    password_hash TEXT,
    role TEXT DEFAULT 'CUSTOMER',

    display_name TEXT,
    phone TEXT,
    email TEXT,

    email_verified_at TEXT,
    email_verification_token TEXT,
    email_verification_expires_at TEXT,
    email_verification_sent_at TEXT,

    password_reset_token TEXT,
    password_reset_expires_at TEXT,
    password_reset_used_at TEXT,

    status TEXT DEFAULT 'ACTIVE',
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_login_id ON users(login_id);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_email_verification_token ON users(email_verification_token);
CREATE INDEX IF NOT EXISTS idx_users_password_reset_token ON users(password_reset_token);
CREATE INDEX IF NOT EXISTS idx_users_role_status ON users(role, status);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);


-- =========================================================
-- stores
-- =========================================================
CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    store_code TEXT UNIQUE,
    owner_user_id INTEGER,

    store_name TEXT,
    phone TEXT,
    address TEXT,
    store_lat REAL DEFAULT 0,
    store_lng REAL DEFAULT 0,

    category TEXT,
    description TEXT,
    banner_url TEXT,

    city_block TEXT DEFAULT 'ZHONGLI',
    area_label TEXT DEFAULT '中壢區',

    is_open INTEGER DEFAULT 1,
    open_time TEXT DEFAULT '10:00',
    close_time TEXT DEFAULT '21:00',
    open_days_json TEXT,
    last_order_minutes_before_close INTEGER DEFAULT 30,

    is_temporarily_closed INTEGER DEFAULT 0,
    temporary_close_reason TEXT,

    status_reason TEXT,
    approved_at TEXT,
    approved_by_admin_id INTEGER DEFAULT 0,

    setup_completed INTEGER DEFAULT 0,

    contract_signed_at TEXT,
    contract_payload_json TEXT,
    contract_hash TEXT,

    -- Phase 6 / payout account
    payout_bank_name TEXT,
    payout_bank_code TEXT,
    payout_bank_account TEXT,
    payout_account_name TEXT,
    payout_note TEXT,

    status TEXT DEFAULT 'ACTIVE',
    created_at TEXT,
    updated_at TEXT,

    FOREIGN KEY(owner_user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_stores_store_code ON stores(store_code);
CREATE INDEX IF NOT EXISTS idx_stores_owner_user_id ON stores(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_stores_status ON stores(status);
CREATE INDEX IF NOT EXISTS idx_stores_city_block ON stores(city_block);
CREATE INDEX IF NOT EXISTS idx_stores_open_status ON stores(is_open, is_temporarily_closed);
CREATE INDEX IF NOT EXISTS idx_stores_business_hours ON stores(open_time, close_time);
CREATE INDEX IF NOT EXISTS idx_stores_status_city ON stores(status, city_block);
CREATE INDEX IF NOT EXISTS idx_stores_is_open ON stores(is_open);


-- =========================================================
-- drivers
-- =========================================================
CREATE TABLE IF NOT EXISTS drivers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    driver_code TEXT UNIQUE,
    user_id INTEGER,

    driver_name TEXT,
    phone TEXT,
    service_area TEXT,
    city_block TEXT DEFAULT 'ZHONGLI',
    area_label TEXT DEFAULT '中壢區',
    vehicle_type TEXT,

    is_online INTEGER DEFAULT 0,
    smartroad_lane TEXT,

    status_reason TEXT,
    approved_at TEXT,
    approved_by_admin_id INTEGER DEFAULT 0,

    contract_signed_at TEXT,
    contract_payload_json TEXT,
    contract_hash TEXT,

    -- Phase 6 / payout account
    payout_bank_name TEXT,
    payout_bank_code TEXT,
    payout_bank_account TEXT,
    payout_account_name TEXT,
    payout_note TEXT,

    status TEXT DEFAULT 'ACTIVE',
    created_at TEXT,
    updated_at TEXT,

    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_drivers_driver_code ON drivers(driver_code);
CREATE INDEX IF NOT EXISTS idx_drivers_user_id ON drivers(user_id);
CREATE INDEX IF NOT EXISTS idx_drivers_status ON drivers(status);
CREATE INDEX IF NOT EXISTS idx_drivers_city_block ON drivers(city_block);
CREATE INDEX IF NOT EXISTS idx_drivers_online_city_status ON drivers(is_online, city_block, status);
CREATE INDEX IF NOT EXISTS idx_drivers_is_online ON drivers(is_online);


-- =========================================================
-- products
-- =========================================================
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    store_id INTEGER,
    name TEXT,
    price_twd INTEGER DEFAULT 0,
    description TEXT,
    image_url TEXT,

    product_category TEXT DEFAULT '主餐',
    sort_order INTEGER DEFAULT 0,
    stock_qty INTEGER DEFAULT 999,
    prepare_minutes INTEGER DEFAULT 15,
    product_note TEXT,

    is_active INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,

    FOREIGN KEY(store_id) REFERENCES stores(id)
);

CREATE INDEX IF NOT EXISTS idx_products_store_id ON products(store_id);
CREATE INDEX IF NOT EXISTS idx_products_active ON products(is_active);
CREATE INDEX IF NOT EXISTS idx_products_is_active ON products(is_active);
CREATE INDEX IF NOT EXISTS idx_products_store_active_sort ON products(store_id, is_active, sort_order, id);


-- =========================================================
-- orders
-- Do not use strict CHECK constraints here.
-- The app uses evolving status/payment states.
-- =========================================================
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_code TEXT UNIQUE,

    customer_user_id INTEGER,
    store_id INTEGER,
    driver_id INTEGER,

    status TEXT DEFAULT 'CREATED',

    payment_method TEXT DEFAULT 'COD',
    payment_status TEXT DEFAULT 'UNPAID',

    delivery_method TEXT DEFAULT 'FACE_TO_FACE',

    subtotal_twd INTEGER DEFAULT 0,
    delivery_fee_twd INTEGER DEFAULT 0,
    base_delivery_fee_twd INTEGER DEFAULT 0,
    customer_delivery_share_twd INTEGER DEFAULT 0,
    store_delivery_support_twd INTEGER DEFAULT 0,
    delivery_fee_rule_json TEXT,

    service_fee_twd INTEGER DEFAULT 0,
    extra_fee_twd INTEGER DEFAULT 0,
    rain_fee_twd INTEGER DEFAULT 0,
    total_twd INTEGER DEFAULT 0,

    delivery_address TEXT,
    delivery_lat REAL DEFAULT 0,
    delivery_lng REAL DEFAULT 0,
    distance_band TEXT DEFAULT '0-2KM',
    floor_number TEXT,
    address_note TEXT,
    extra_fee_reason TEXT,
    difficulty_flags_json TEXT,

    customer_name TEXT,
    customer_phone TEXT,
    note TEXT,
    proof_image_url TEXT,

    -- Customer receipt/invoice request note.
    invoice_required INTEGER DEFAULT 0,
    invoice_type TEXT DEFAULT 'NONE',
    invoice_title TEXT,
    invoice_tax_id TEXT,
    invoice_note TEXT,

    -- Payment proof / admin review.
    payment_proof_image_url TEXT,
    payment_proof_uploaded_at TEXT,
    payment_proof_status TEXT DEFAULT 'PENDING_REVIEW',
    payment_proof_reviewed_at TEXT,
    payment_proof_reviewed_by_admin_id INTEGER,

    payment_verified_at TEXT,
    payment_verified_by INTEGER,
    payment_rejected_at TEXT,
    payment_reject_reason TEXT,

    -- Delivery proof.
    delivery_proof_image_url TEXT,
    delivery_proof_uploaded_at TEXT,
    delivery_proof_sent_email_at TEXT,

    -- Return-to-store proof.
    return_proof_image_url TEXT,
    return_proof_uploaded_at TEXT,

    city_block TEXT DEFAULT 'ZHONGLI',
    area_label TEXT DEFAULT '中壢區',

    smartroad_lane TEXT,
    distance_km REAL DEFAULT 0,
    smartroad_score INTEGER DEFAULT 50,
    smartroad_score_label TEXT DEFAULT 'UNKNOWN',
    smartroad_reasons_json TEXT,
    smartroad_same_road INTEGER DEFAULT 0,
    smartroad_same_side INTEGER DEFAULT 0,
    smartroad_uturn_risk INTEGER DEFAULT 0,

    store_road_name TEXT,
    customer_road_name TEXT,
    store_house_number TEXT,
    customer_house_number TEXT,
    store_house_parity TEXT,
    customer_house_parity TEXT,

    admin_hold INTEGER DEFAULT 0,
    admin_hold_reason TEXT,
    admin_hold_at TEXT,

    order_source TEXT DEFAULT 'CUSTOMER_MARKETPLACE',
    store_created_by INTEGER,
    manual_order_title TEXT,
    prepaid_to TEXT,

    created_at TEXT,
    updated_at TEXT,

    FOREIGN KEY(customer_user_id) REFERENCES users(id),
    FOREIGN KEY(store_id) REFERENCES stores(id),
    FOREIGN KEY(driver_id) REFERENCES drivers(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_order_code ON orders(order_code);
CREATE INDEX IF NOT EXISTS idx_orders_store_id ON orders(store_id);
CREATE INDEX IF NOT EXISTS idx_orders_driver_id ON orders(driver_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_admin_hold ON orders(admin_hold);
CREATE INDEX IF NOT EXISTS idx_orders_city_block ON orders(city_block);
CREATE INDEX IF NOT EXISTS idx_orders_order_source ON orders(order_source);
CREATE INDEX IF NOT EXISTS idx_orders_store_created_by ON orders(store_created_by);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_updated_at ON orders(updated_at);

CREATE INDEX IF NOT EXISTS idx_orders_payment_method ON orders(payment_method);
CREATE INDEX IF NOT EXISTS idx_orders_payment_status ON orders(payment_status);
CREATE INDEX IF NOT EXISTS idx_orders_invoice_required ON orders(invoice_required);
CREATE INDEX IF NOT EXISTS idx_orders_invoice_type ON orders(invoice_type);

CREATE INDEX IF NOT EXISTS idx_orders_payment_proof_status ON orders(payment_proof_status);
CREATE INDEX IF NOT EXISTS idx_orders_payment_proof_uploaded_at ON orders(payment_proof_uploaded_at);
CREATE INDEX IF NOT EXISTS idx_orders_payment_verified_at ON orders(payment_verified_at);
CREATE INDEX IF NOT EXISTS idx_orders_payment_verified_by ON orders(payment_verified_by);
CREATE INDEX IF NOT EXISTS idx_orders_payment_rejected_at ON orders(payment_rejected_at);
CREATE INDEX IF NOT EXISTS idx_orders_payment_pending_review ON orders(payment_method, payment_status, admin_hold, payment_proof_status);

CREATE INDEX IF NOT EXISTS idx_orders_delivery_proof_uploaded_at ON orders(delivery_proof_uploaded_at);

CREATE INDEX IF NOT EXISTS idx_orders_store_status_hold_id ON orders(store_id, status, admin_hold, id);
CREATE INDEX IF NOT EXISTS idx_orders_store_status_updated ON orders(store_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_orders_store_hold_status_updated ON orders(store_id, admin_hold, status, updated_at);

CREATE INDEX IF NOT EXISTS idx_orders_waiting_city_hold_driver_id ON orders(status, city_block, admin_hold, driver_id, id);
CREATE INDEX IF NOT EXISTS idx_orders_waiting_driver_null_city ON orders(status, driver_id, city_block, admin_hold, id);

CREATE INDEX IF NOT EXISTS idx_orders_driver_status_id ON orders(driver_id, status, id);
CREATE INDEX IF NOT EXISTS idx_orders_driver_status_updated ON orders(driver_id, status, updated_at);

CREATE INDEX IF NOT EXISTS idx_orders_distance_band ON orders(distance_band);
CREATE INDEX IF NOT EXISTS idx_orders_distance_km ON orders(distance_km);
CREATE INDEX IF NOT EXISTS idx_orders_smartroad_score ON orders(smartroad_score);
CREATE INDEX IF NOT EXISTS idx_orders_smartroad_same_road ON orders(smartroad_same_road);
CREATE INDEX IF NOT EXISTS idx_orders_smartroad_uturn_risk ON orders(smartroad_uturn_risk);
CREATE INDEX IF NOT EXISTS idx_orders_city_status_smartroad ON orders(city_block, status, smartroad_score);


-- =========================================================
-- order_items
-- =========================================================
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    order_id INTEGER,
    product_id INTEGER,
    product_name TEXT,
    unit_price_twd INTEGER DEFAULT 0,
    qty INTEGER DEFAULT 1,
    line_total_twd INTEGER DEFAULT 0,
    created_at TEXT,

    FOREIGN KEY(order_id) REFERENCES orders(id),
    FOREIGN KEY(product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product_id ON order_items(product_id);


-- =========================================================
-- line_contact_bindings
-- =========================================================
CREATE TABLE IF NOT EXISTS line_contact_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    role TEXT,
    target_code TEXT,
    contact_code TEXT UNIQUE,
    line_user_id TEXT,
    line_display_name TEXT,

    status TEXT DEFAULT 'ACTIVE',
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_line_bindings_role_target ON line_contact_bindings(role, target_code);
CREATE INDEX IF NOT EXISTS idx_line_bindings_contact_code ON line_contact_bindings(contact_code);
CREATE INDEX IF NOT EXISTS idx_line_bindings_line_user_id ON line_contact_bindings(line_user_id);


-- =========================================================
-- line_push_logs
-- =========================================================
CREATE TABLE IF NOT EXISTS line_push_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    contact_code TEXT,
    line_user_id TEXT,
    event_type TEXT,
    target_role TEXT,
    target_code TEXT,
    order_code TEXT,
    message_preview TEXT,
    push_status TEXT,
    gateway_response TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_line_push_logs_contact_code ON line_push_logs(contact_code);
CREATE INDEX IF NOT EXISTS idx_line_push_logs_order_code ON line_push_logs(order_code);
CREATE INDEX IF NOT EXISTS idx_line_push_logs_target ON line_push_logs(target_role, target_code);
CREATE INDEX IF NOT EXISTS idx_line_push_logs_created_at ON line_push_logs(created_at);


-- =========================================================
-- blocks
-- =========================================================
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    block_code TEXT UNIQUE,
    event_type TEXT,

    actor_role TEXT,
    actor_id INTEGER,
    actor_code TEXT,

    order_id INTEGER,
    order_code TEXT,

    previous_status TEXT,
    new_status TEXT,

    amount_twd INTEGER DEFAULT 0,

    payload_json TEXT,
    payload_hash TEXT,

    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_blocks_event_type ON blocks(event_type);
CREATE INDEX IF NOT EXISTS idx_blocks_order_code ON blocks(order_code);
CREATE INDEX IF NOT EXISTS idx_blocks_order_code_id ON blocks(order_code, id);
CREATE INDEX IF NOT EXISTS idx_blocks_actor ON blocks(actor_role, actor_code);
CREATE INDEX IF NOT EXISTS idx_blocks_created_at ON blocks(created_at);


-- =========================================================
-- accounting_entries
-- Important:
-- direction must support INFO / CREDIT / DEBIT.
-- Do not restrict to IN / OUT.
-- =========================================================
CREATE TABLE IF NOT EXISTS accounting_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    entry_code TEXT UNIQUE,
    order_id INTEGER,
    order_code TEXT,

    entry_type TEXT,
    role TEXT,
    target_code TEXT,

    amount_twd INTEGER DEFAULT 0,
    direction TEXT,
    note TEXT,

    created_at TEXT,

    FOREIGN KEY(order_id) REFERENCES orders(id)
);

CREATE INDEX IF NOT EXISTS idx_accounting_entries_order_code ON accounting_entries(order_code);
CREATE INDEX IF NOT EXISTS idx_accounting_entries_role_target ON accounting_entries(role, target_code);
CREATE INDEX IF NOT EXISTS idx_accounting_entries_created_at ON accounting_entries(created_at);

CREATE INDEX IF NOT EXISTS idx_accounting_order_code_id ON accounting_entries(order_code, id);
CREATE INDEX IF NOT EXISTS idx_accounting_target ON accounting_entries(role, target_code);
CREATE INDEX IF NOT EXISTS idx_accounting_created_at ON accounting_entries(created_at);
CREATE INDEX IF NOT EXISTS idx_accounting_entry_type ON accounting_entries(entry_type);


-- =========================================================
-- settlement_confirmations
-- Legacy / V1 confirmation table
-- =========================================================
CREATE TABLE IF NOT EXISTS settlement_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    confirmation_code TEXT UNIQUE,

    order_id INTEGER,
    order_code TEXT,

    confirmation_type TEXT,

    payer_role TEXT,
    payer_code TEXT,
    receiver_role TEXT,
    receiver_code TEXT,

    amount_twd INTEGER DEFAULT 0,
    status TEXT DEFAULT 'CONFIRMED',
    note TEXT,

    admin_user_id INTEGER DEFAULT 0,
    admin_login_id TEXT,

    created_at TEXT,
    updated_at TEXT,

    FOREIGN KEY(order_id) REFERENCES orders(id)
);

CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_order_code ON settlement_confirmations(order_code);
CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_type ON settlement_confirmations(confirmation_type);
CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_payer ON settlement_confirmations(payer_role, payer_code);
CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_receiver ON settlement_confirmations(receiver_role, receiver_code);
CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_created_at ON settlement_confirmations(created_at);


-- =========================================================
-- settlement_batches
-- Phase 6 Lite
-- =========================================================
CREATE TABLE IF NOT EXISTS settlement_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    settlement_code TEXT UNIQUE,

    role TEXT,
    target_code TEXT,
    target_user_id INTEGER,
    target_email TEXT,

    direction TEXT,
    settlement_type TEXT,

    period_start TEXT,
    period_end TEXT,

    amount_twd INTEGER DEFAULT 0,
    status TEXT DEFAULT 'DRAFT',

    email_sent_at TEXT,

    paid_confirmed_at TEXT,
    paid_confirmed_by INTEGER,

    payment_method TEXT DEFAULT 'BANK_TRANSFER',

    admin_bank_snapshot_json TEXT,
    target_payout_snapshot_json TEXT,

    -- Phase 6 Lite:
    -- Store/Driver reports paid. This is not final settlement.
    -- Only Admin confirm-paid may change status to PAID_CONFIRMED.
    target_marked_paid_at TEXT,
    target_marked_paid_note TEXT,
    target_payment_method TEXT,
    target_payment_proof_image_url TEXT,

    related_order_codes_json TEXT,
    note TEXT,

    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_settlement_batches_code ON settlement_batches(settlement_code);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_role_target ON settlement_batches(role, target_code);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_direction_type ON settlement_batches(direction, settlement_type);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_status ON settlement_batches(status);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_period ON settlement_batches(period_start, period_end);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_created_at ON settlement_batches(created_at);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_target_email ON settlement_batches(target_email);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_target_marked_paid_at ON settlement_batches(target_marked_paid_at);


-- =========================================================
-- system_flags
-- =========================================================
CREATE TABLE IF NOT EXISTS system_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    flag_key TEXT UNIQUE,
    flag_value TEXT DEFAULT '0',
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_system_flags_key ON system_flags(flag_key);


-- =========================================================
-- email_logs
-- =========================================================
CREATE TABLE IF NOT EXISTS email_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    event_type TEXT,
    recipient_email TEXT,
    recipient_role TEXT,
    user_id INTEGER,

    order_id INTEGER,
    order_code TEXT,

    subject TEXT,
    status TEXT,
    error_message TEXT,
    provider_message_id TEXT,

    retry_count INTEGER DEFAULT 0,
    last_attempt_at TEXT,
    created_at TEXT,

    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(order_id) REFERENCES orders(id)
);

CREATE INDEX IF NOT EXISTS idx_email_logs_order_code ON email_logs(order_code);
CREATE INDEX IF NOT EXISTS idx_email_logs_recipient ON email_logs(recipient_email);
CREATE INDEX IF NOT EXISTS idx_email_logs_event_type ON email_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_email_logs_created_at ON email_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_email_logs_status ON email_logs(status);

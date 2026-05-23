CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login_id TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('CUSTOMER','STORE','DRIVER','ADMIN_OPERATOR')),
    display_name TEXT,
    phone TEXT,
    email TEXT,
    email_verified_at TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_code TEXT UNIQUE NOT NULL,
    owner_user_id INTEGER,
    store_name TEXT NOT NULL,
    phone TEXT,
    address TEXT,
    category TEXT,
    description TEXT,
    banner_url TEXT,
    is_open INTEGER NOT NULL DEFAULT 1,
    setup_completed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(owner_user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_stores_owner_user_id ON stores(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_stores_status ON stores(status);
CREATE INDEX IF NOT EXISTS idx_stores_is_open ON stores(is_open);

CREATE TABLE IF NOT EXISTS drivers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_code TEXT UNIQUE NOT NULL,
    user_id INTEGER,
    driver_name TEXT NOT NULL,
    phone TEXT,
    service_area TEXT,
    vehicle_type TEXT,
    is_online INTEGER NOT NULL DEFAULT 0,
    smartroad_lane TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_drivers_user_id ON drivers(user_id);
CREATE INDEX IF NOT EXISTS idx_drivers_status ON drivers(status);
CREATE INDEX IF NOT EXISTS idx_drivers_is_online ON drivers(is_online);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    price_twd INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    image_url TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(store_id) REFERENCES stores(id)
);

CREATE INDEX IF NOT EXISTS idx_products_store_id ON products(store_id);
CREATE INDEX IF NOT EXISTS idx_products_is_active ON products(is_active);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code TEXT UNIQUE NOT NULL,

    customer_user_id INTEGER,
    store_id INTEGER NOT NULL,
    driver_id INTEGER,

    status TEXT NOT NULL DEFAULT 'CREATED'
        CHECK(status IN (
            'CREATED',
            'STORE_ACCEPTED',
            'WAITING_DRIVER',
            'DRIVER_ACCEPTED',
            'PICKED_UP',
            'DELIVERY_ISSUE',
            'RETURNING_TO_STORE',
            'RETURNED_TO_STORE',
            'DELIVERED',
            'COMPLETED',
            'CANCELLED',
            'DISPUTED'
        )),

    payment_method TEXT NOT NULL DEFAULT 'COD'
        CHECK(payment_method IN (
            'COD',
            'BANK_TRANSFER',
            'PLATFORM',
            'PREPAID_TO_STORE'
        )),

    payment_status TEXT NOT NULL DEFAULT 'UNPAID'
        CHECK(payment_status IN (
            'UNPAID',
            'PENDING',
            'PENDING_REUPLOAD',
            'PAID',
            'FAILED'
        )),

    delivery_method TEXT NOT NULL DEFAULT 'FACE_TO_FACE'
        CHECK(delivery_method IN ('FACE_TO_FACE','PHOTO_PROOF')),

    subtotal_twd INTEGER NOT NULL DEFAULT 0,
    delivery_fee_twd INTEGER NOT NULL DEFAULT 0,
    service_fee_twd INTEGER NOT NULL DEFAULT 0,
    total_twd INTEGER NOT NULL DEFAULT 0,

    delivery_address TEXT NOT NULL,
    customer_name TEXT,
    customer_phone TEXT,
    customer_email TEXT,
    guest_access_token TEXT,

    note TEXT,
    proof_image_url TEXT,

    smartroad_lane TEXT,
    distance_km REAL DEFAULT 0,

    order_source TEXT DEFAULT 'CUSTOMER_MARKETPLACE',

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY(customer_user_id) REFERENCES users(id),
    FOREIGN KEY(store_id) REFERENCES stores(id),
    FOREIGN KEY(driver_id) REFERENCES drivers(id)
);

CREATE INDEX IF NOT EXISTS idx_orders_order_code ON orders(order_code);
CREATE INDEX IF NOT EXISTS idx_orders_store_id ON orders(store_id);
CREATE INDEX IF NOT EXISTS idx_orders_driver_id ON orders(driver_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_customer_email ON orders(customer_email);
CREATE INDEX IF NOT EXISTS idx_orders_guest_access_token ON orders(guest_access_token);
CREATE INDEX IF NOT EXISTS idx_orders_guest_tracking ON orders(order_code, guest_access_token);
CREATE INDEX IF NOT EXISTS idx_orders_order_source ON orders(order_source);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    product_id INTEGER,
    product_name TEXT NOT NULL,
    unit_price_twd INTEGER NOT NULL DEFAULT 0,
    qty INTEGER NOT NULL DEFAULT 1,
    line_total_twd INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES orders(id),
    FOREIGN KEY(product_id) REFERENCES products(id)
);

CREATE INDEX IF NOT EXISTS idx_order_items_order_id ON order_items(order_id);

CREATE TABLE IF NOT EXISTS line_contact_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK(role IN ('CUSTOMER','STORE','DRIVER','ADMIN')),
    target_code TEXT NOT NULL,
    contact_code TEXT UNIQUE NOT NULL,
    line_user_id TEXT NOT NULL,
    line_display_name TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','DISABLED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_line_bindings_role_target ON line_contact_bindings(role, target_code);
CREATE INDEX IF NOT EXISTS idx_line_bindings_line_user_id ON line_contact_bindings(line_user_id);
CREATE INDEX IF NOT EXISTS idx_line_bindings_contact_code ON line_contact_bindings(contact_code);

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
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_line_push_logs_contact_code ON line_push_logs(contact_code);
CREATE INDEX IF NOT EXISTS idx_line_push_logs_order_code ON line_push_logs(order_code);
CREATE INDEX IF NOT EXISTS idx_line_push_logs_created_at ON line_push_logs(created_at);

CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    block_code TEXT UNIQUE NOT NULL,

    event_type TEXT NOT NULL,

    actor_role TEXT NOT NULL,
    actor_id INTEGER,
    actor_code TEXT,

    order_id INTEGER,
    order_code TEXT,

    previous_status TEXT,
    new_status TEXT,

    amount_twd INTEGER NOT NULL DEFAULT 0,

    payload_json TEXT,
    payload_hash TEXT NOT NULL,

    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_blocks_event_type ON blocks(event_type);
CREATE INDEX IF NOT EXISTS idx_blocks_order_code ON blocks(order_code);
CREATE INDEX IF NOT EXISTS idx_blocks_created_at ON blocks(created_at);

CREATE TABLE IF NOT EXISTS accounting_entries (
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
);

CREATE INDEX IF NOT EXISTS idx_accounting_entries_order_code ON accounting_entries(order_code);
CREATE INDEX IF NOT EXISTS idx_accounting_entries_role_target ON accounting_entries(role, target_code);
CREATE INDEX IF NOT EXISTS idx_accounting_entries_created_at ON accounting_entries(created_at);

CREATE TABLE IF NOT EXISTS settlement_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    confirmation_code TEXT,
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
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_order_code ON settlement_confirmations(order_code);
CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_payer ON settlement_confirmations(payer_role, payer_code);
CREATE INDEX IF NOT EXISTS idx_settlement_confirmations_receiver ON settlement_confirmations(receiver_role, receiver_code);

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
    related_order_codes_json TEXT,
    note TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_settlement_batches_code ON settlement_batches(settlement_code);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_role_target ON settlement_batches(role, target_code);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_status ON settlement_batches(status);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_period ON settlement_batches(period_start, period_end);

CREATE TABLE IF NOT EXISTS system_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_key TEXT UNIQUE,
    flag_value TEXT DEFAULT '0',
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_system_flags_key ON system_flags(flag_key);

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
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_email_logs_event_type ON email_logs(event_type);
CREATE INDEX IF NOT EXISTS idx_email_logs_recipient_email ON email_logs(recipient_email);
CREATE INDEX IF NOT EXISTS idx_email_logs_order_code ON email_logs(order_code);
CREATE INDEX IF NOT EXISTS idx_email_logs_created_at ON email_logs(created_at);

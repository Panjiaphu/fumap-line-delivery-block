import secrets

from services.code_service import now_iso, unique_code
from services.block_service import create_block


VALID_BIND_ROLES = {"CUSTOMER", "STORE", "DRIVER", "ADMIN", "ADMIN_OPERATOR"}


class LineBindError(ValueError):
    pass


def normalize_role(role: str) -> str:
    role = (role or "").strip().upper()

    if role == "ADMIN_OPERATOR":
        return "ADMIN"

    return role


def validate_bind_role(role: str) -> str:
    role = normalize_role(role)

    if role not in {"CUSTOMER", "STORE", "DRIVER", "ADMIN"}:
        raise LineBindError("LINE 綁定只支援 CUSTOMER / STORE / DRIVER / ADMIN。")

    return role


def _row_get(row, key, default=""):
    if row is None:
        return default

    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except Exception:
        pass

    try:
        value = row.get(key, default)
        return default if value is None else value
    except Exception:
        return default


def _clean_text(value, default=""):
    value = "" if value is None else str(value)
    value = value.strip()
    return value if value else default


def _generate_contact_code_for_role(role: str) -> str:
    """
    Generate a human-safe LINE contact code.

    Existing project uses:
    - CUSTOMER: CUM
    - STORE: STRO
    - DRIVER: DRV
    New:
    - ADMIN: ADM
    """
    prefix = prefix_for_role(role)
    return f"{prefix}-{secrets.token_hex(3).upper()}"


def target_code_for_user(db, user):
    """
    Return the operational target code for current user.

    CUSTOMER:
      - CUS-{user_id}

    STORE:
      - stores.store_code

    DRIVER:
      - drivers.driver_code

    ADMIN:
      - ADMIN
    """
    if not user:
        raise LineBindError("請先登入。")

    role = validate_bind_role(user["role"])

    if role == "CUSTOMER":
        return f"CUS-{user['id']}"

    if role == "ADMIN":
        return "ADMIN"

    if role == "STORE":
        store = db.execute(
            """
            SELECT *
            FROM stores
            WHERE owner_user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user["id"],),
        ).fetchone()

        if not store:
            raise LineBindError("找不到店家資料，請先完成店家註冊。")

        return store["store_code"]

    if role == "DRIVER":
        driver = db.execute(
            """
            SELECT *
            FROM drivers
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user["id"],),
        ).fetchone()

        if not driver:
            raise LineBindError("找不到 Shiper 資料，請先完成 Shiper 註冊。")

        return driver["driver_code"]

    raise LineBindError("不支援的角色。")


def label_for_role(role: str) -> str:
    role = normalize_role(role)

    if role == "CUSTOMER":
        return "客戶"

    if role == "STORE":
        return "店家"

    if role == "DRIVER":
        return "Shiper"

    if role == "ADMIN":
        return "Admin"

    return role or "未知"


def prefix_for_role(role: str) -> str:
    role = validate_bind_role(role)

    if role == "CUSTOMER":
        return "CUM"

    if role == "STORE":
        return "STRO"

    if role == "DRIVER":
        return "DRV"

    if role == "ADMIN":
        return "ADM"

    return "CNT"


def get_binding_by_role_target(db, role: str, target_code: str):
    role = validate_bind_role(role)
    target_code = (target_code or "").strip().upper()

    if not target_code:
        return None

    return db.execute(
        """
        SELECT *
        FROM line_contact_bindings
        WHERE role = ?
          AND target_code = ?
        ORDER BY
          CASE WHEN status = 'ACTIVE' THEN 0 ELSE 1 END,
          id DESC
        LIMIT 1
        """,
        (role, target_code),
    ).fetchone()


def get_active_binding_by_role_target(db, role: str, target_code: str):
    role = validate_bind_role(role)
    target_code = (target_code or "").strip().upper()

    if not target_code:
        return None

    return db.execute(
        """
        SELECT *
        FROM line_contact_bindings
        WHERE role = ?
          AND target_code = ?
          AND status = 'ACTIVE'
          AND line_user_id IS NOT NULL
          AND line_user_id != ''
        ORDER BY id DESC
        LIMIT 1
        """,
        (role, target_code),
    ).fetchone()


def get_active_line_binding(db, role: str, target_code: str):
    return get_active_binding_by_role_target(db, role, target_code)


def get_active_binding_by_contact_code(db, contact_code: str):
    contact_code = (contact_code or "").strip().upper()

    if not contact_code:
        return None

    return db.execute(
        """
        SELECT *
        FROM line_contact_bindings
        WHERE contact_code = ?
          AND status = 'ACTIVE'
          AND line_user_id IS NOT NULL
          AND line_user_id != ''
        ORDER BY id DESC
        LIMIT 1
        """,
        (contact_code,),
    ).fetchone()


def get_binding_by_contact_code(db, contact_code: str):
    contact_code = (contact_code or "").strip().upper()

    if not contact_code:
        return None

    return db.execute(
        """
        SELECT *
        FROM line_contact_bindings
        WHERE contact_code = ?
        ORDER BY
          CASE WHEN status = 'ACTIVE' THEN 0 ELSE 1 END,
          id DESC
        LIMIT 1
        """,
        (contact_code,),
    ).fetchone()


def get_active_binding_by_line_user(db, role: str, line_user_id: str):
    role = validate_bind_role(role)
    line_user_id = (line_user_id or "").strip()

    if not line_user_id:
        return None

    return db.execute(
        """
        SELECT *
        FROM line_contact_bindings
        WHERE role = ?
          AND line_user_id = ?
          AND status = 'ACTIVE'
        ORDER BY id DESC
        LIMIT 1
        """,
        (role, line_user_id),
    ).fetchone()


def list_bindings(db, limit=100):
    return db.execute(
        """
        SELECT *
        FROM line_contact_bindings
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit or 100),),
    ).fetchall()


def list_bindings_by_role(db, role: str, limit=100):
    role = validate_bind_role(role)

    return db.execute(
        """
        SELECT *
        FROM line_contact_bindings
        WHERE role = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (role, int(limit or 100)),
    ).fetchall()


def _disable_duplicate_same_role_line(db, *, keep_id, role, line_user_id):
    """
    Same LINE user may bind multiple roles.
    But same LINE user should not have duplicate active binding for same role.
    """
    role = validate_bind_role(role)
    line_user_id = (line_user_id or "").strip()

    if not line_user_id:
        return

    db.execute(
        """
        UPDATE line_contact_bindings
        SET status = 'DISABLED',
            updated_at = ?
        WHERE role = ?
          AND line_user_id = ?
          AND id <> ?
          AND status = 'ACTIVE'
        """,
        (now_iso(), role, line_user_id, keep_id),
    )


def _upsert_binding(
    db,
    *,
    user,
    role,
    target_code,
    line_user_id,
    line_display_name="",
    event_type="LINE_BOUND",
    commit=True,
):
    if not user:
        raise LineBindError("請先登入 webapp，再綁定 LINE。")

    role = validate_bind_role(role)
    target_code = (target_code or "").strip().upper()
    line_user_id = (line_user_id or "").strip()
    line_display_name = (line_display_name or "").strip()

    if not target_code:
        raise LineBindError("找不到綁定目標代碼。")

    if not line_user_id:
        raise LineBindError("缺少 LINE userId。")

    if not line_user_id.startswith("U"):
        raise LineBindError("LINE userId 格式不正確。")

    now = now_iso()
    existing = get_binding_by_role_target(db, role, target_code)

    if existing:
        contact_code = existing["contact_code"]

        if not contact_code:
            contact_code = unique_code(
                db,
                "line_contact_bindings",
                "contact_code",
                lambda: _generate_contact_code_for_role(role),
            )

        db.execute(
            """
            UPDATE line_contact_bindings
            SET contact_code = ?,
                line_user_id = ?,
                line_display_name = ?,
                status = 'ACTIVE',
                updated_at = ?
            WHERE id = ?
            """,
            (
                contact_code,
                line_user_id,
                line_display_name,
                now,
                existing["id"],
            ),
        )

        keep_id = existing["id"]
        previous_status = existing["status"] or ""

    else:
        contact_code = unique_code(
            db,
            "line_contact_bindings",
            "contact_code",
            lambda: _generate_contact_code_for_role(role),
        )

        cur = db.execute(
            """
            INSERT INTO line_contact_bindings (
                role,
                target_code,
                contact_code,
                line_user_id,
                line_display_name,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            """,
            (
                role,
                target_code,
                contact_code,
                line_user_id,
                line_display_name,
                now,
                now,
            ),
        )

        keep_id = cur.lastrowid
        previous_status = ""

    _disable_duplicate_same_role_line(
        db,
        keep_id=keep_id,
        role=role,
        line_user_id=line_user_id,
    )

    create_block(
        db,
        event_type=event_type,
        actor_role=role,
        actor_id=user["id"],
        actor_code=target_code,
        order_id=None,
        order_code="",
        previous_status=previous_status,
        new_status="ACTIVE",
        amount_twd=0,
        payload={
            "role": role,
            "target_code": target_code,
            "contact_code": contact_code,
            "line_user_id": line_user_id,
            "line_display_name": line_display_name,
            "bind_source": "LIFF_ONE_TAP" if event_type == "LINE_BIND_ONE_TAP_SUCCESS" else "WEB_MANUAL",
            "note": "LINE notification contact only. No login, no account creation, no permission grant.",
        },
        commit=False,
    )

    if commit:
        db.commit()

    return get_binding_by_role_target(db, role, target_code)


def bind_line_contact(
    db,
    *,
    user,
    line_user_id,
    line_display_name="",
    commit=True,
):
    """
    Backward-compatible bind function.

    Old route may still call this function.
    New One-Tap route should call bind_line_user_to_current_account().
    """
    if not user:
        raise LineBindError("請先登入 webapp，再綁定 LINE。")

    role = validate_bind_role(user["role"])
    target_code = target_code_for_user(db, user)

    return _upsert_binding(
        db,
        user=user,
        role=role,
        target_code=target_code,
        line_user_id=line_user_id,
        line_display_name=line_display_name,
        event_type="LINE_BOUND",
        commit=commit,
    )


def bind_line_user_to_current_account(
    db,
    *,
    user,
    line_user_id,
    line_display_name="",
    picture_url="",
    commit=True,
):
    """
    One-Tap LIFF bind.

    Hard rules:
    - Does not create account.
    - Does not login.
    - Does not grant permission.
    - Does not create store/driver.
    - Only binds current webapp account to LINE userId for notifications.
    """
    if not user:
        raise LineBindError("請先登入 webapp，再綁定 LINE。")

    role = validate_bind_role(user["role"])
    target_code = target_code_for_user(db, user)

    binding = _upsert_binding(
        db,
        user=user,
        role=role,
        target_code=target_code,
        line_user_id=line_user_id,
        line_display_name=line_display_name,
        event_type="LINE_BIND_ONE_TAP_SUCCESS",
        commit=commit,
    )

    return binding


def disable_binding(db, *, user, commit=True):
    if not user:
        raise LineBindError("請先登入。")

    role = validate_bind_role(user["role"])
    target_code = target_code_for_user(db, user)
    now = now_iso()

    binding = get_binding_by_role_target(db, role, target_code)

    if not binding:
        raise LineBindError("目前沒有 LINE 綁定。")

    db.execute(
        """
        UPDATE line_contact_bindings
        SET status = 'DISABLED',
            updated_at = ?
        WHERE id = ?
        """,
        (now, binding["id"]),
    )

    create_block(
        db,
        event_type="LINE_UNBOUND",
        actor_role=role,
        actor_id=user["id"],
        actor_code=target_code,
        previous_status=binding["status"],
        new_status="DISABLED",
        payload={
            "role": role,
            "target_code": target_code,
            "contact_code": binding["contact_code"],
            "line_user_id": binding["line_user_id"],
        },
        commit=False,
    )

    if commit:
        db.commit()

    return True


def current_user_binding(db, user):
    if not user:
        return None

    role = validate_bind_role(user["role"])
    target_code = target_code_for_user(db, user)

    return get_binding_by_role_target(db, role, target_code)


def current_user_line_context(db, user):
    if not user:
        return {
            "role": "",
            "role_label": "",
            "target_code": "",
            "binding": None,
            "active": False,
            "contact_code": "",
            "line_display_name": "",
        }

    role = validate_bind_role(user["role"])
    target_code = target_code_for_user(db, user)
    binding = get_binding_by_role_target(db, role, target_code)
    active = bool(binding and binding["status"] == "ACTIVE" and binding["line_user_id"])

    return {
        "role": role,
        "role_label": label_for_role(role),
        "target_code": target_code,
        "binding": binding,
        "active": active,
        "contact_code": binding["contact_code"] if binding else "",
        "line_display_name": binding["line_display_name"] if binding else "",
    }


def customer_can_photo_proof(db, user):
    """
    Backward-compatible helper.

    Payment V1C no longer uses LINE bind as BANK_TRANSFER gate.
    This function only reflects LINE bind availability.
    """
    if not user or user["role"] != "CUSTOMER":
        return False

    ctx = current_user_line_context(db, user)
    return bool(ctx["active"])

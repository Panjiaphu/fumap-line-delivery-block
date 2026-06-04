import hashlib
import secrets
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash, check_password_hash

from services.code_service import (
    now_iso,
    unique_code,
    generate_store_code,
    generate_driver_code,
)


VALID_ROLES = {"CUSTOMER", "STORE", "DRIVER", "ADMIN_OPERATOR"}
REGISTER_ROLES = {"CUSTOMER", "STORE", "DRIVER"}

PASSWORD_RESET_MINUTES = 30
EMAIL_VERIFICATION_HOURS = 24
EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS = 60


class AuthError(ValueError):
    pass


def normalize_role(role: str) -> str:
    return (role or "").strip().upper()


def normalize_login_id(login_id: str) -> str:
    return (login_id or "").strip().lower()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email_optional(email: str) -> str:
    email = normalize_email(email)

    if not email:
        return ""

    if "@" not in email or "." not in email.split("@")[-1]:
        raise AuthError("Email 格式不正確。")

    if len(email) > 255:
        raise AuthError("Email 過長。")

    return email


def validate_role(role: str, allow_admin: bool = False) -> str:
    role = normalize_role(role)

    allowed = VALID_ROLES if allow_admin else REGISTER_ROLES

    if role not in allowed:
        raise AuthError("角色不正確。")

    return role


def validate_registration_payload(login_id, password, display_name, phone, role, email=""):
    login_id = normalize_login_id(login_id)
    role = validate_role(role, allow_admin=False)
    display_name = (display_name or "").strip()
    phone = (phone or "").strip()
    email = validate_email_optional(email)

    if not login_id:
        raise AuthError("請輸入登入 ID。")

    if len(login_id) < 3:
        raise AuthError("登入 ID 至少 3 個字。")

    if not password or len(password) < 6:
        raise AuthError("密碼至少 6 位。")

    if not display_name:
        raise AuthError("請輸入名稱。")

    if role in {"CUSTOMER", "STORE", "DRIVER"} and not phone:
        raise AuthError("請輸入電話。")

    return {
        "login_id": login_id,
        "password": password,
        "display_name": display_name,
        "phone": phone,
        "email": email,
        "role": role,
    }


def _sha256(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _parse_iso(value):
    try:
        return datetime.fromisoformat((value or "").replace("Z", ""))
    except Exception:
        return None


def _now_dt():
    return datetime.now()


def _expires_at(minutes=PASSWORD_RESET_MINUTES):
    return (_now_dt() + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _email_verification_expires_at():
    return (_now_dt() + timedelta(hours=EMAIL_VERIFICATION_HOURS)).isoformat(timespec="seconds")


def _row_has(row, key):
    try:
        return key in row.keys()
    except Exception:
        return False


def _row_get(row, key, default=None):
    if row is None:
        return default

    try:
        if key in row.keys():
            return row[key]
    except Exception:
        pass

    try:
        return row.get(key, default)
    except Exception:
        return default


def get_user_by_id(db, user_id):
    if not user_id:
        return None

    return db.execute(
        """
        SELECT *
        FROM users
        WHERE id = ?
        """,
        (user_id,),
    ).fetchone()


def get_user_by_login_id(db, login_id):
    login_id = normalize_login_id(login_id)

    return db.execute(
        """
        SELECT *
        FROM users
        WHERE login_id = ?
        """,
        (login_id,),
    ).fetchone()


def get_user_by_email(db, email):
    email = normalize_email(email)

    if not email:
        return None

    return db.execute(
        """
        SELECT *
        FROM users
        WHERE lower(COALESCE(email, '')) = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (email,),
    ).fetchone()


def get_store_by_user_id(db, user_id):
    return db.execute(
        """
        SELECT *
        FROM stores
        WHERE owner_user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()


def get_driver_by_user_id(db, user_id):
    return db.execute(
        """
        SELECT *
        FROM drivers
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()


def get_role_target(db, user):
    if not user:
        return None

    role = user["role"]

    if role == "STORE":
        return get_store_by_user_id(db, user["id"])

    if role == "DRIVER":
        return get_driver_by_user_id(db, user["id"])

    return None


def register_user(
    db,
    *,
    login_id,
    password,
    display_name,
    phone,
    role,
    email="",
    register_ip="",
    user_agent="",
    risk_score=0,
):
    data = validate_registration_payload(
        login_id=login_id,
        password=password,
        display_name=display_name,
        phone=phone,
        role=role,
        email=email,
    )

    exists = get_user_by_login_id(db, data["login_id"])

    if exists:
        raise AuthError("此登入 ID 已存在。")

    if data["email"]:
        email_exists = get_user_by_email(db, data["email"])

        if email_exists:
            raise AuthError("此 Email 已被使用。請使用其他 Email 或使用忘記密碼。")

    now = now_iso()
    password_hash = generate_password_hash(data["password"])

    cur = db.execute(
        """
        INSERT INTO users (
            login_id,
            password_hash,
            role,
            display_name,
            phone,
            email,
            status,
            register_ip,
            user_agent,
            risk_score,
            verify_send_count,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, 0, ?, ?)
        """,
        (
            data["login_id"],
            password_hash,
            data["role"],
            data["display_name"],
            data["phone"],
            data["email"],
            (register_ip or "")[:120],
            (user_agent or "")[:500],
            int(risk_score or 0),
            now,
            now,
        ),
    )

    user_id = cur.lastrowid

    if data["role"] == "STORE":
        store_code = unique_code(db, "stores", "store_code", generate_store_code)

        db.execute(
            """
            INSERT INTO stores (
                store_code,
                owner_user_id,
                store_name,
                phone,
                address,
                category,
                description,
                is_open,
                setup_completed,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, '', '', '', 1, 0, 'ACTIVE', ?, ?)
            """,
            (
                store_code,
                user_id,
                data["display_name"],
                data["phone"],
                now,
                now,
            ),
        )

    elif data["role"] == "DRIVER":
        driver_code = unique_code(db, "drivers", "driver_code", generate_driver_code)

        db.execute(
            """
            INSERT INTO drivers (
                driver_code,
                user_id,
                driver_name,
                phone,
                service_area,
                vehicle_type,
                is_online,
                smartroad_lane,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, '', '', 0, '', 'ACTIVE', ?, ?)
            """,
            (
                driver_code,
                user_id,
                data["display_name"],
                data["phone"],
                now,
                now,
            ),
        )

    db.commit()

    return get_user_by_id(db, user_id)


def authenticate_user(db, login_id, password):
    login_id = normalize_login_id(login_id)

    if not login_id or not password:
        raise AuthError("請輸入登入 ID 與密碼。")

    user = get_user_by_login_id(db, login_id)

    if not user:
        raise AuthError("帳號或密碼錯誤。")

    if user["status"] != "ACTIVE":
        raise AuthError("帳號已停用，請聯絡管理員。")

    if not check_password_hash(user["password_hash"], password):
        raise AuthError("帳號或密碼錯誤。")

    return user


def authenticate_admin(config, login_id, password):
    admin_username = (config.get("ADMIN_USERNAME") or "admin").strip()
    admin_password = (config.get("ADMIN_PASSWORD") or "admin123").strip()

    if (login_id or "").strip() == admin_username and (password or "").strip() == admin_password:
        return {
            "id": 0,
            "login_id": admin_username,
            "role": "ADMIN_OPERATOR",
            "display_name": "Admin",
            "phone": "",
            "status": "ACTIVE",
        }

    return None


def create_password_reset_token(db, email):
    email = validate_email_optional(email)

    if not email:
        return None

    user = get_user_by_email(db, email)

    if not user:
        return None

    raw_token = secrets.token_urlsafe(32)
    token_hash = _sha256(raw_token)
    now = now_iso()
    expires_at = _expires_at(PASSWORD_RESET_MINUTES)

    db.execute(
        """
        UPDATE users
        SET password_reset_token = ?,
            password_reset_expires_at = ?,
            password_reset_used_at = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (
            token_hash,
            expires_at,
            now,
            user["id"],
        ),
    )
    db.commit()

    return {
        "user": get_user_by_id(db, user["id"]),
        "raw_token": raw_token,
        "expires_at": expires_at,
    }


def get_user_by_valid_reset_token(db, raw_token):
    raw_token = (raw_token or "").strip()

    if not raw_token:
        return None

    token_hash = _sha256(raw_token)

    user = db.execute(
        """
        SELECT *
        FROM users
        WHERE password_reset_token = ?
          AND password_reset_used_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (token_hash,),
    ).fetchone()

    if not user:
        return None

    expires_at = _parse_iso(user["password_reset_expires_at"])

    if not expires_at:
        return None

    if expires_at < _now_dt():
        return None

    return user


def reset_password_with_token(db, raw_token, new_password, confirm_password):
    user = get_user_by_valid_reset_token(db, raw_token)

    if not user:
        raise AuthError("連結已失效，請重新申請。")

    new_password = new_password or ""
    confirm_password = confirm_password or ""

    if len(new_password) < 6:
        raise AuthError("新密碼至少 6 位。")

    if new_password != confirm_password:
        raise AuthError("兩次密碼不一致。")

    now = now_iso()
    password_hash = generate_password_hash(new_password)

    db.execute(
        """
        UPDATE users
        SET password_hash = ?,
            password_reset_token = NULL,
            password_reset_expires_at = NULL,
            password_reset_used_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            password_hash,
            now,
            now,
            user["id"],
        ),
    )
    db.commit()

    return get_user_by_id(db, user["id"])


def create_email_verification_token(db, user_id):
    user = get_user_by_id(db, user_id)

    if not user:
        return None

    email = normalize_email(_row_get(user, "email", ""))

    if not email:
        return None

    if _row_get(user, "email_verified_at", ""):
        return None

    raw_token = secrets.token_urlsafe(32)
    token_hash = _sha256(raw_token)
    now = now_iso()
    expires_at = _email_verification_expires_at()

    db.execute(
        """
        UPDATE users
        SET email_verification_token = ?,
            email_verification_expires_at = ?,
            email_verification_sent_at = ?,
            last_verify_sent_at = ?,
            verify_send_count = COALESCE(verify_send_count, 0) + 1,
            updated_at = ?
        WHERE id = ?
        """,
        (
            token_hash,
            expires_at,
            now,
            now,
            now,
            user["id"],
        ),
    )
    db.commit()

    return {
        "user": get_user_by_id(db, user["id"]),
        "raw_token": raw_token,
        "expires_at": expires_at,
    }


def get_user_by_valid_email_verification_token(db, raw_token):
    raw_token = (raw_token or "").strip()

    if not raw_token:
        return None

    token_hash = _sha256(raw_token)

    user = db.execute(
        """
        SELECT *
        FROM users
        WHERE email_verification_token = ?
          AND email_verified_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (token_hash,),
    ).fetchone()

    if not user:
        return None

    expires_at = _parse_iso(_row_get(user, "email_verification_expires_at", ""))

    if not expires_at:
        return None

    if expires_at < _now_dt():
        return None

    return user


def verify_email_with_token(db, raw_token):
    user = get_user_by_valid_email_verification_token(db, raw_token)

    if not user:
        raise AuthError("驗證連結已失效，請重新申請。")

    now = now_iso()

    db.execute(
        """
        UPDATE users
        SET email_verified_at = ?,
            email_verification_token = NULL,
            email_verification_expires_at = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (
            now,
            now,
            user["id"],
        ),
    )
    db.commit()

    return get_user_by_id(db, user["id"])


def can_resend_email_verification(user):
    if not user:
        return False, "請重新登入。"

    if not normalize_email(_row_get(user, "email", "")):
        return False, "此帳號尚未設定 Email。"

    if _row_get(user, "email_verified_at", ""):
        return False, "Email 已驗證。"

    sent_at = _parse_iso(_row_get(user, "email_verification_sent_at", ""))

    if not sent_at:
        return True, ""

    elapsed = (_now_dt() - sent_at).total_seconds()

    if elapsed < EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS:
        remain = int(EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS - elapsed)
        return False, f"請稍候 {remain} 秒後再重新寄送。"

    return True, ""


def change_user_password(db, user_id, current_password, new_password, confirm_password):
    user = get_user_by_id(db, user_id)

    if not user:
        raise AuthError("找不到帳號，請重新登入。")

    current_password = current_password or ""
    new_password = new_password or ""
    confirm_password = confirm_password or ""

    if not current_password:
        raise AuthError("請輸入目前密碼。")

    if not check_password_hash(user["password_hash"], current_password):
        raise AuthError("目前密碼不正確。")

    if len(new_password) < 6:
        raise AuthError("新密碼至少 6 位。")

    if new_password != confirm_password:
        raise AuthError("兩次新密碼不一致。")

    if current_password == new_password:
        raise AuthError("新密碼不能與目前密碼相同。")

    now = now_iso()
    password_hash = generate_password_hash(new_password)

    db.execute(
        """
        UPDATE users
        SET password_hash = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            password_hash,
            now,
            user_id,
        ),
    )
    db.commit()

    return get_user_by_id(db, user_id)


def session_payload(user, target=None):
    payload = {
        "user_id": user["id"],
        "login_id": user["login_id"],
        "role": user["role"],
        "display_name": user["display_name"] or user["login_id"],
        "phone": user["phone"] or "",
        "target_code": "",
    }

    if user["role"] == "STORE" and target:
        payload["target_code"] = target["store_code"]

    if user["role"] == "DRIVER" and target:
        payload["target_code"] = target["driver_code"]

    return payload

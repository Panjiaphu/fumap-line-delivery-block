import secrets
import string
from datetime import datetime, timezone, timedelta


TAIPEI_TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M:%S")


def today_yyyymmdd() -> str:
    return datetime.now(TAIPEI_TZ).strftime("%Y%m%d")


def short_token(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_code(prefix: str, length: int = 8) -> str:
    return f"{prefix}-{short_token(length)}"


def generate_user_public_code(role: str) -> str:
    role = (role or "").upper().strip()

    if role == "CUSTOMER":
        return generate_code("CUS", 8)

    if role == "STORE":
        return generate_code("STORE", 8)

    if role == "DRIVER":
        return generate_code("DRVUSER", 8)

    return generate_code("USER", 8)


def generate_store_code() -> str:
    return generate_code("STO", 8)


def generate_driver_code() -> str:
    return generate_code("DRV", 8)


def generate_order_code() -> str:
    return f"FGO-{today_yyyymmdd()}-{short_token(5)}"


def generate_block_code() -> str:
    return f"BLK-{today_yyyymmdd()}-{short_token(8)}"


def generate_accounting_entry_code() -> str:
    return f"ACC-{today_yyyymmdd()}-{short_token(8)}"


def generate_contact_code(role: str) -> str:
    role = (role or "").upper().strip()

    if role == "CUSTOMER":
        return generate_code("CUM", 8)

    if role == "STORE":
        return generate_code("STRO", 8)

    if role == "DRIVER":
        return generate_code("DRV", 8)

    return generate_code("CNT", 8)


def unique_code(db, table: str, column: str, generator, max_attempts: int = 30) -> str:
    for _ in range(max_attempts):
        code = generator()
        row = db.execute(
            f"SELECT id FROM {table} WHERE {column} = ? LIMIT 1",
            (code,),
        ).fetchone()

        if not row:
            return code

    raise RuntimeError(f"Cannot generate unique code for {table}.{column}")

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def get_bool_env(key, default=False):
    value = os.getenv(key, "").strip().lower()

    if value in {"1", "true", "yes", "on"}:
        return True

    if value in {"0", "false", "no", "off"}:
        return False

    return default


def get_int_env(key, default):
    try:
        return int(os.getenv(key, str(default)))
    except Exception:
        return int(default)


def get_float_env(key, default):
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return float(default)


class Config:
    APP_NAME = os.getenv("APP_NAME", "FUMAP GO")
    APP_ENV = os.getenv("FLASK_ENV", os.getenv("APP_MODE", "production"))
    SECRET_KEY = os.getenv(
        "SECRET_KEY",
        os.getenv("FLASK_SECRET_KEY", "fumap-go-dev-secret"),
    )

    DATABASE_PATH = os.getenv(
        "DATABASE_PATH",
        os.getenv("SQLITE_PATH", str(BASE_DIR / "fumap_go.sqlite3")),
    )

    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", os.getenv("ADMIN_PASS", "admin123"))
    ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

    LINE_GATEWAY_BASE_URL = os.getenv("LINE_GATEWAY_BASE_URL", "").rstrip("/")
    FGO_INTERNAL_SECRET = os.getenv("FGO_INTERNAL_SECRET", "")

    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    APP_BASE_URL = os.getenv(
        "APP_BASE_URL",
        os.getenv("PUBLIC_BASE_URL", ""),
    ).rstrip("/")

    PERMANENT_SESSION_LIFETIME_DAYS = get_int_env(
        "PERMANENT_SESSION_LIFETIME_DAYS",
        30,
    )

    # Email / SMTP config.
    # V1 can use Gmail App Password for testing.
    # Production should later move to a transactional provider such as
    # Postmark, Amazon SES, Mailgun, SendGrid, or Brevo.
    SMTP_HOST = os.getenv("SMTP_HOST", os.getenv("MAIL_SERVER", "")).strip()
    SMTP_PORT = get_int_env("SMTP_PORT", get_int_env("MAIL_PORT", 587))
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", os.getenv("MAIL_USERNAME", "")).strip()
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", os.getenv("MAIL_PASSWORD", "")).strip()
    SMTP_USE_TLS = get_bool_env("SMTP_USE_TLS", get_bool_env("MAIL_USE_TLS", True))
    SMTP_FROM_EMAIL = os.getenv(
        "SMTP_FROM_EMAIL",
        os.getenv("MAIL_DEFAULT_SENDER", SMTP_USERNAME),
    ).strip()

    # Admin notification contact config.
    ADMIN_NOTIFY_EMAIL = os.getenv(
        "ADMIN_NOTIFY_EMAIL",
        "panjiaphu@gmail.com",
    ).strip()

    ADMIN_NOTIFY_EMAILS = os.getenv(
        "ADMIN_NOTIFY_EMAILS",
        ADMIN_NOTIFY_EMAIL,
    ).strip()

    LINE_ADMIN_ID = os.getenv(
        "LINE_ADMIN_ID",
        "@827sxbki",
    ).strip()

    LINE_ADMIN_URL = os.getenv(
        "LINE_ADMIN_URL",
        "https://line.me/R/ti/p/@827sxbki",
    ).strip()

    TZ = os.getenv("TZ", "Asia/Taipei")
    DEFAULT_LANG = os.getenv("DEFAULT_LANG", "zh")

    MAX_DELIVERY_DISTANCE_KM = get_float_env("MAX_DELIVERY_DISTANCE_KM", 5)

    DEFAULT_SERVICE_FEE_TWD = get_int_env("DEFAULT_SERVICE_FEE_TWD", 10)
    DEFAULT_DELIVERY_FEE_TWD = get_int_env("DEFAULT_DELIVERY_FEE_TWD", 60)

    # Bước 7A đã dùng CUSTOMER_SERVICE_FEE_TWD để khách không bị cộng service fee trực tiếp.
    CUSTOMER_SERVICE_FEE_TWD = get_int_env("CUSTOMER_SERVICE_FEE_TWD", 0)

    # Delivery extra fee config.
    DELIVERY_EXTRA_FEE_TWD = get_int_env("DELIVERY_EXTRA_FEE_TWD", 20)
    DELIVERY_MAX_KM = get_float_env("DELIVERY_MAX_KM", 5)

    # Platform payment info for BANK_TRANSFER checkout.
    PLATFORM_BANK_NAME = os.getenv("PLATFORM_BANK_NAME", "").strip()
    PLATFORM_BANK_CODE = os.getenv("PLATFORM_BANK_CODE", "").strip()
    PLATFORM_BANK_ACCOUNT = os.getenv("PLATFORM_BANK_ACCOUNT", "").strip()
    PLATFORM_BANK_NOTE = os.getenv("PLATFORM_BANK_NOTE", "").strip()
    PLATFORM_LINEPAY_NAME = os.getenv("PLATFORM_LINEPAY_NAME", "平台銀行轉帳").strip()
    PLATFORM_LINEPAY_QR_URL = os.getenv("PLATFORM_LINEPAY_QR_URL", "").strip()
    PLATFORM_PAYMENT_ACCOUNT = os.getenv("PLATFORM_PAYMENT_ACCOUNT", "").strip()

    # Image upload / compression config.
    # Default: use static/uploads for simple Render demo.
    # Later can change UPLOAD_ROOT to persistent disk, e.g. /var/data/uploads.
    UPLOAD_MAX_MB = get_int_env("UPLOAD_MAX_MB", 5)
    IMAGE_WEBP_QUALITY = get_int_env("IMAGE_WEBP_QUALITY", 72)

    STORE_BANNER_MAX_WIDTH = get_int_env("STORE_BANNER_MAX_WIDTH", 1200)
    STORE_BANNER_MAX_HEIGHT = get_int_env("STORE_BANNER_MAX_HEIGHT", 600)

    PRODUCT_IMAGE_MAX_WIDTH = get_int_env("PRODUCT_IMAGE_MAX_WIDTH", 800)
    PRODUCT_IMAGE_MAX_HEIGHT = get_int_env("PRODUCT_IMAGE_MAX_HEIGHT", 800)

    UPLOAD_ROOT = os.getenv(
        "UPLOAD_ROOT",
        os.getenv("UPLOAD_DIR", str(BASE_DIR / "static" / "uploads")),
    )
    UPLOAD_URL_PREFIX = os.getenv("UPLOAD_URL_PREFIX", "/static/uploads").rstrip("/")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = get_bool_env("SESSION_COOKIE_SECURE", False)


config = Config()

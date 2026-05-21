from pathlib import Path
from urllib.parse import unquote

from flask import current_app


ALLOWED_PROOF_TYPES = {"payment", "delivery", "return", "legacy"}


class ProofAccessError(PermissionError):
    pass


class ProofNotFoundError(FileNotFoundError):
    pass


def _row_get(row, key, default=None):
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


def _as_int(value, default=0):
    try:
        return int(value or default)
    except Exception:
        return int(default)


def _normalize_order_code(order_code):
    return (order_code or "").strip().upper()


def _normalize_proof_type(proof_type):
    return (proof_type or "").strip().lower()


def get_order_for_proof(db, order_code):
    """
    Load order plus owner information needed for role-based proof access.
    """
    order_code = _normalize_order_code(order_code)

    if not order_code:
        return None

    return db.execute(
        """
        SELECT
            o.*,

            s.owner_user_id AS store_owner_user_id,
            s.store_code AS proof_store_code,
            s.store_name AS proof_store_name,

            d.user_id AS driver_user_id,
            d.driver_code AS proof_driver_code
        FROM orders o
        LEFT JOIN stores s ON s.id = o.store_id
        LEFT JOIN drivers d ON d.id = o.driver_id
        WHERE o.order_code = ?
        LIMIT 1
        """,
        (order_code,),
    ).fetchone()


def proof_url_for_type(order, proof_type):
    """
    Map proof type to order proof URL.
    """
    proof_type = _normalize_proof_type(proof_type)

    if proof_type not in ALLOWED_PROOF_TYPES:
        return ""

    if proof_type == "payment":
        return _row_get(order, "payment_proof_image_url", "")

    if proof_type == "delivery":
        return _row_get(order, "delivery_proof_image_url", "")

    if proof_type == "return":
        return _row_get(order, "return_proof_image_url", "")

    if proof_type == "legacy":
        return _row_get(order, "proof_image_url", "")

    return ""


def can_view_order_proof(user, order, proof_type):
    """
    Role-based proof access.

    ADMIN_OPERATOR:
      can view all proof images.

    CUSTOMER:
      only orders owned by this user.

    STORE:
      only orders belonging to the store owned by this user.

    DRIVER:
      only orders assigned to driver profile linked to this user.
    """
    if not user or not order:
        return False

    role = (_row_get(user, "role", "") or "").upper()
    user_id = _as_int(_row_get(user, "id", 0))

    if role == "ADMIN_OPERATOR":
        return True

    if role == "CUSTOMER":
        return _as_int(_row_get(order, "customer_user_id", 0)) == user_id

    if role == "STORE":
        return _as_int(_row_get(order, "store_owner_user_id", 0)) == user_id

    if role == "DRIVER":
        return _as_int(_row_get(order, "driver_user_id", 0)) == user_id

    return False


def _configured_upload_root():
    root = Path(str(current_app.config.get("UPLOAD_ROOT") or "static/uploads"))

    if not root.is_absolute():
        root = Path(current_app.root_path) / root

    return root.resolve()


def _static_upload_root():
    return (Path(current_app.root_path) / "static" / "uploads").resolve()


def _upload_url_prefixes():
    configured = str(
        current_app.config.get("UPLOAD_URL_PREFIX") or "/static/uploads"
    ).rstrip("/")

    prefixes = []

    if configured:
        prefixes.append(configured)

    for fallback in ["/uploads", "/static/uploads"]:
        if fallback not in prefixes:
            prefixes.append(fallback)

    return prefixes


def _safe_join_inside(root, relative_path):
    root = root.resolve()
    candidate = (root / relative_path).resolve()

    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ProofAccessError("Invalid proof path.") from exc

    return candidate


def local_path_from_upload_url(url):
    """
    Convert stored upload URL to local file path safely.

    Accepted:
    - /uploads/proofs/xxx.webp
    - /static/uploads/proofs/xxx.webp
    - configured UPLOAD_URL_PREFIX + /proofs/xxx.webp

    Rejected:
    - empty
    - http:// or https:// external URL
    - path traversal
    - non-proof folders
    - missing files
    """
    url = unquote(str(url or "").strip())

    if not url:
        raise ProofNotFoundError("Missing proof URL.")

    lowered = url.lower()

    if lowered.startswith("http://") or lowered.startswith("https://"):
        raise ProofAccessError("External proof URL is not allowed.")

    if "?" in url:
        url = url.split("?", 1)[0]

    if "#" in url:
        url = url.split("#", 1)[0]

    if "\\" in url:
        raise ProofAccessError("Invalid path separator.")

    if ".." in Path(url).parts:
        raise ProofAccessError("Path traversal is not allowed.")

    upload_root = _configured_upload_root()
    static_root = _static_upload_root()

    matched_prefix = None

    for prefix in _upload_url_prefixes():
        prefix = prefix.rstrip("/")

        if url == prefix or url.startswith(prefix + "/"):
            matched_prefix = prefix
            break

    if not matched_prefix:
        raise ProofAccessError("Unsupported proof URL prefix.")

    relative = url[len(matched_prefix):].lstrip("/")

    if not relative:
        raise ProofNotFoundError("Missing proof file path.")

    relative_path = Path(relative)

    if not relative_path.parts or relative_path.parts[0] != "proofs":
        raise ProofAccessError("Only proof files are protected by this viewer.")

    roots_to_try = []

    if matched_prefix == "/static/uploads":
        roots_to_try.append(static_root)
        roots_to_try.append(upload_root)
    else:
        roots_to_try.append(upload_root)
        roots_to_try.append(static_root)

    checked = []

    for root in roots_to_try:
        try:
            candidate = _safe_join_inside(root, relative_path)
            checked.append(candidate)

            if candidate.exists() and candidate.is_file():
                return candidate

        except ProofAccessError:
            raise

    raise ProofNotFoundError("Proof file not found.")

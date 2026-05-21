import secrets
from pathlib import Path

from flask import current_app
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename


ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}


class ImageUploadError(ValueError):
    pass


def _int_config(key, default):
    try:
        return int(current_app.config.get(key, default))
    except Exception:
        return int(default)


def _str_config(key, default=""):
    value = current_app.config.get(key, default)
    return str(value or default).strip()


def upload_root() -> Path:
    root = Path(_str_config("UPLOAD_ROOT", "static/uploads"))

    if not root.is_absolute():
        root = Path(current_app.root_path) / root

    root.mkdir(parents=True, exist_ok=True)
    return root


def upload_url_prefix() -> str:
    return _str_config("UPLOAD_URL_PREFIX", "/static/uploads").rstrip("/")


def ensure_upload_dirs():
    root = upload_root()

    for folder in ("stores", "products", "proofs", "tmp"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    return root


def allowed_extension(filename: str) -> bool:
    if not filename or "." not in filename:
        return False

    ext = filename.rsplit(".", 1)[-1].lower().strip()
    return ext in ALLOWED_EXTENSIONS


def validate_upload_file(file: FileStorage):
    if not file or not isinstance(file, FileStorage):
        raise ImageUploadError("沒有收到圖片檔案。")

    if not file.filename:
        raise ImageUploadError("請選擇圖片檔案。")

    if not allowed_extension(file.filename):
        raise ImageUploadError("圖片格式只支援 JPG、PNG、WEBP、GIF。")

    max_mb = _int_config("UPLOAD_MAX_MB", 5)
    max_bytes = max_mb * 1024 * 1024

    stream = file.stream
    current_pos = stream.tell()

    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(current_pos)

    if size <= 0:
        raise ImageUploadError("圖片檔案是空的。")

    if size > max_bytes:
        raise ImageUploadError(f"圖片太大，請上傳 {max_mb}MB 以下圖片。")

    try:
        stream.seek(0)
        with Image.open(stream) as image:
            image.verify()
    except UnidentifiedImageError as exc:
        raise ImageUploadError("圖片內容格式不支援。") from exc
    except Exception as exc:
        raise ImageUploadError(f"圖片驗證失敗：{exc}") from exc
    finally:
        stream.seek(0)


def _target_config(kind: str):
    kind = (kind or "").strip().lower()

    if kind == "store_banner":
        return {
            "folder": "stores",
            "prefix": "store-banner",
            "max_width": _int_config("STORE_BANNER_MAX_WIDTH", 1200),
            "max_height": _int_config("STORE_BANNER_MAX_HEIGHT", 600),
        }

    if kind == "product_image":
        return {
            "folder": "products",
            "prefix": "product",
            "max_width": _int_config("PRODUCT_IMAGE_MAX_WIDTH", 800),
            "max_height": _int_config("PRODUCT_IMAGE_MAX_HEIGHT", 800),
        }

    if kind == "proof_image":
        return {
            "folder": "proofs",
            "prefix": "proof",
            "max_width": 1200,
            "max_height": 1200,
        }

    raise ImageUploadError("未知圖片類型。")


def _normalize_image(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)

    if image.mode in {"RGBA", "LA", "P"}:
        background = Image.new("RGB", image.size, (255, 255, 255))

        if image.mode == "P":
            image = image.convert("RGBA")

        if image.mode in {"RGBA", "LA"}:
            background.paste(image, mask=image.split()[-1])
            image = background
        else:
            image = image.convert("RGB")

    elif image.mode != "RGB":
        image = image.convert("RGB")

    return image


def _resize_for_box(image: Image.Image, max_width: int, max_height: int) -> Image.Image:
    width, height = image.size

    if width <= max_width and height <= max_height:
        return image

    image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    return image


def save_compressed_upload(file: FileStorage, *, kind: str, owner_code: str = "") -> str:
    """
    Validate, resize and convert an uploaded image to WEBP.

    Returns public URL:
    - /static/uploads/products/xxx.webp
    - /static/uploads/stores/xxx.webp
    - /static/uploads/proofs/xxx.webp
    """
    validate_upload_file(file)
    ensure_upload_dirs()

    target = _target_config(kind)
    quality = _int_config("IMAGE_WEBP_QUALITY", 72)

    safe_owner = secure_filename(str(owner_code or "owner")).lower()[:40] or "owner"
    token = secrets.token_hex(8)

    filename = f"{target['prefix']}-{safe_owner}-{token}.webp"

    root = upload_root()
    folder = root / target["folder"]
    folder.mkdir(parents=True, exist_ok=True)

    output_path = folder / filename

    try:
        file.stream.seek(0)

        with Image.open(file.stream) as image:
            image = _normalize_image(image)
            image = _resize_for_box(
                image,
                int(target["max_width"]),
                int(target["max_height"]),
            )

            image.save(
                output_path,
                format="WEBP",
                quality=quality,
                method=6,
                optimize=True,
            )

    except ImageUploadError:
        raise

    except UnidentifiedImageError as exc:
        raise ImageUploadError("圖片內容格式不支援。") from exc

    except Exception as exc:
        raise ImageUploadError(f"圖片處理失敗：{exc}") from exc

    return f"{upload_url_prefix()}/{target['folder']}/{filename}"


def maybe_save_compressed_upload(file: FileStorage, *, kind: str, owner_code: str = "") -> str:
    """
    Optional upload helper.

    Returns:
    - "" if no file selected
    - uploaded WEBP URL if file exists
    """
    if not file or not getattr(file, "filename", ""):
        return ""

    return save_compressed_upload(file, kind=kind, owner_code=owner_code)

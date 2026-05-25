import os
import traceback
from datetime import timedelta

from flask import Flask, session, request, send_from_directory, jsonify, abort

from config import Config
from db import close_db, init_db, get_db


def _bool_text(value):
    return "true" if bool(value) else "false"


def _config_set(app, key):
    value = app.config.get(key, "")
    return bool(str(value or "").strip())


def _env_text(key, default=""):
    return str(os.getenv(key, default) or "").strip()


def _render_git_commit():
    return (
        _env_text("RENDER_GIT_COMMIT")
        or _env_text("GIT_COMMIT")
        or _env_text("COMMIT_SHA")
        or "unknown"
    )


def _render_service_name():
    return (
        _env_text("RENDER_SERVICE_NAME")
        or _env_text("RENDER_SERVICE_ID")
        or "unknown"
    )


def _print_route_map(app):
    """
    Print all registered routes at boot.

    Purpose:
    - Confirm Render is running the correct app.py.
    - Confirm /, /show, /store, /store/realtime/status, /v2/events/poll exist.
    - Detect missing blueprint registration.
    """
    print("[BOOT][ROUTES] ------------------------------")

    for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
        methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        print(f"[BOOT][ROUTES] {methods:10s} {rule.rule:40s} -> {rule.endpoint}")

    print("[BOOT][ROUTES] ------------------------------")


def register_boot_diagnostics(app):
    """
    Render boot diagnostics.

    Rules:
    - Print only safe values.
    - Never print secrets.
    - Help detect wrong commit / wrong branch / wrong DB / missing LINE ENV.
    """
    linehook_set = bool(
        str(app.config.get("LINEHOOK_BASE_URL", "") or "").strip()
        or str(app.config.get("LINE_GATEWAY_BASE_URL", "") or "").strip()
    )

    print("[BOOT][DIAG] ------------------------------")
    print(f"[BOOT][DIAG] app_name={app.config.get('APP_NAME', 'FUMAP GO')}")
    print(f"[BOOT][DIAG] app_env={app.config.get('APP_ENV', '')}")
    print(f"[BOOT][DIAG] render_service={_render_service_name()}")
    print(f"[BOOT][DIAG] render_git_commit={_render_git_commit()}")
    print(f"[BOOT][DIAG] database_path={app.config.get('DATABASE_PATH', '')}")
    print(f"[BOOT][DIAG] public_base_url={app.config.get('PUBLIC_BASE_URL', '')}")
    print(f"[BOOT][DIAG] app_base_url={app.config.get('APP_BASE_URL', '')}")

    print(f"[BOOT][DIAG] line_liff_id_set={_bool_text(_config_set(app, 'LINE_LIFF_ID'))}")
    print(f"[BOOT][DIAG] linehook_base_url_set={_bool_text(linehook_set)}")
    print(
        "[BOOT][DIAG] fgo_admin_line_user_id_set="
        f"{_bool_text(_config_set(app, 'FGO_ADMIN_LINE_USER_ID'))}"
    )

    print(
        "[BOOT][DIAG] session_cookie_secure="
        f"{_bool_text(app.config.get('SESSION_COOKIE_SECURE', False))}"
    )
    print(
        "[BOOT][DIAG] session_cookie_samesite="
        f"{app.config.get('SESSION_COOKIE_SAMESITE', '')}"
    )
    print("[BOOT][DIAG] ------------------------------")


def register_upload_routes(app):
    """
    Serve uploaded files from persistent disk.

    Render production env:
    - UPLOAD_ROOT=/var/data/uploads
    - UPLOAD_URL_PREFIX=/uploads
    """

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        safe_name = (filename or "").replace("\\", "/").lstrip("/")

        if safe_name.startswith("proofs/"):
            abort(403)

        return send_from_directory(app.config["UPLOAD_ROOT"], safe_name)


def register_health_routes(app):
    """
    Lightweight health endpoints for Render / uptime checks.

    /health:
      Does not touch database. Use for basic process health.

    /health/db:
      Touches SQLite with SELECT 1. Use manually or for deeper checks.

    /health/render:
      Safe Render/runtime diagnostics. Does not expose secrets.
    """

    @app.get("/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "app": app.config.get("APP_NAME", "FUMAP GO"),
                "status": "healthy",
                "render_git_commit": _render_git_commit(),
            }
        )

    @app.get("/health/db")
    def health_db():
        try:
            db = get_db()
            db.execute("SELECT 1").fetchone()

            return jsonify(
                {
                    "ok": True,
                    "app": app.config.get("APP_NAME", "FUMAP GO"),
                    "status": "healthy",
                    "db": "ok",
                    "database_path": app.config.get("DATABASE_PATH", ""),
                    "render_git_commit": _render_git_commit(),
                }
            )
        except Exception as exc:
            return jsonify(
                {
                    "ok": False,
                    "app": app.config.get("APP_NAME", "FUMAP GO"),
                    "status": "unhealthy",
                    "db": "error",
                    "error": str(exc),
                    "render_git_commit": _render_git_commit(),
                }
            ), 500

    @app.get("/health/render")
    def health_render():
        linehook_set = bool(
            str(app.config.get("LINEHOOK_BASE_URL", "") or "").strip()
            or str(app.config.get("LINE_GATEWAY_BASE_URL", "") or "").strip()
        )

        return jsonify(
            {
                "ok": True,
                "app": app.config.get("APP_NAME", "FUMAP GO"),
                "app_env": app.config.get("APP_ENV", ""),
                "render_service": _render_service_name(),
                "render_git_commit": _render_git_commit(),
                "database_path": app.config.get("DATABASE_PATH", ""),
                "public_base_url": app.config.get("PUBLIC_BASE_URL", ""),
                "app_base_url": app.config.get("APP_BASE_URL", ""),

                "line_liff_id_set": _config_set(app, "LINE_LIFF_ID"),
                "linehook_base_url_set": linehook_set,
                "admin_line_user_id_set": _config_set(app, "FGO_ADMIN_LINE_USER_ID"),

                "session_cookie_secure": bool(app.config.get("SESSION_COOKIE_SECURE", False)),
                "session_cookie_samesite": app.config.get("SESSION_COOKIE_SAMESITE", ""),
            }
        )


def register_routes(app):
    from routes.public_routes import public_bp

    app.register_blueprint(public_bp)
    print("[BOOT] registered blueprint: routes.public_routes.public_bp")

    optional_blueprints = [
        ("routes.auth_routes", "auth_bp"),
        ("routes.customer_routes", "customer_bp"),
        ("routes.store_routes", "store_bp"),
        ("routes.driver_routes", "driver_bp"),
        ("routes.admin_routes", "admin_bp"),
        ("routes.line_routes", "line_bp"),
        ("routes.block_routes", "block_bp"),
        ("routes.proof_routes", "proof_bp"),
    ]

    for module_name, blueprint_name in optional_blueprints:
        try:
            module = __import__(module_name, fromlist=[blueprint_name])
            blueprint = getattr(module, blueprint_name)
            app.register_blueprint(blueprint)
            print(f"[BOOT] registered blueprint: {module_name}.{blueprint_name}")
        except Exception as exc:
            print(
                f"[BOOT][ERROR] failed to register blueprint: "
                f"{module_name}.{blueprint_name}: {exc}"
            )
            traceback.print_exc()


def register_context(app):
    @app.context_processor
    def inject_globals():
        role = session.get("role", "")
        user_id = session.get("user_id")
        display_name = session.get("display_name", "")

        is_logged_in = user_id is not None or role == "ADMIN_OPERATOR"

        return {
            "APP_NAME": app.config.get("APP_NAME", "FUMAP GO"),
            "current_role": role,
            "current_user_id": user_id,
            "current_display_name": display_name,
            "is_logged_in": is_logged_in,
            "is_admin": role == "ADMIN_OPERATOR",
            "request_path": request.path,
        }

    @app.template_filter("twd")
    def twd(value):
        try:
            amount = int(value or 0)
        except Exception:
            amount = 0

        return f"{amount:,} TWD"


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    register_boot_diagnostics(app)

    app.permanent_session_lifetime = timedelta(
        days=int(app.config.get("PERMANENT_SESSION_LIFETIME_DAYS", 30))
    )

    app.teardown_appcontext(close_db)

    register_upload_routes(app)
    register_health_routes(app)

    with app.app_context():
        init_db(app)

    register_context(app)
    register_routes(app)
    _print_route_map(app)

    return app


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
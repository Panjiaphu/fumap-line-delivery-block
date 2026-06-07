import traceback
from datetime import timedelta

from flask import Flask, session, request, send_from_directory, jsonify, abort

from config import Config
from db import close_db, init_db, get_db
from services.abuse_guard import apply_abuse_route_limits, ensure_abuse_schema, init_abuse_guards
from services.availability_session_service import ensure_availability_session_schema
from services.bounce_service import ensure_bounce_schema
from services.firewall_service import ensure_firewall_schema, init_firewall
from services.timeblock_gateway_service import ensure_timeblock_gateway_schema


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    app.permanent_session_lifetime = timedelta(
        days=int(app.config.get("PERMANENT_SESSION_LIFETIME_DAYS", 30))
    )

    init_abuse_guards(app)
    init_firewall(app)
    app.teardown_appcontext(close_db)

    register_upload_routes(app)
    register_health_routes(app)

    with app.app_context():
        init_db(app)
        ensure_abuse_schema(app)
        ensure_firewall_schema(app)
        ensure_bounce_schema(app)
        ensure_timeblock_gateway_schema(app)
        ensure_availability_session_schema(app)

    register_context(app)
    register_routes(app)
    apply_abuse_route_limits(app)

    return app


def register_upload_routes(app):
    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        safe_name = (filename or "").replace("\\", "/").lstrip("/")
        if safe_name.startswith("proofs/"):
            abort(403)
        return send_from_directory(app.config["UPLOAD_ROOT"], safe_name)


def register_health_routes(app):
    @app.get("/health")
    def health():
        return jsonify({"ok": True, "app": app.config.get("APP_NAME", "FUMAP GO"), "status": "healthy"})

    @app.get("/health/db")
    def health_db():
        try:
            db = get_db()
            db.execute("SELECT 1").fetchone()
            return jsonify({"ok": True, "app": app.config.get("APP_NAME", "FUMAP GO"), "status": "healthy", "db": "ok", "database_path": app.config.get("DATABASE_PATH", "")})
        except Exception as exc:
            return jsonify({"ok": False, "app": app.config.get("APP_NAME", "FUMAP GO"), "status": "unhealthy", "db": "error", "error": str(exc)}), 500


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
        ("routes.admin_timeblock_routes", "admin_timeblock_bp"),
        ("routes.timeblock_gateway_routes", "timeblock_gateway_bp"),
        ("routes.availability_routes", "availability_bp"),
        ("routes.admin_abuse_routes", "admin_abuse_bp"),
        ("routes.admin_firewall_routes", "admin_firewall_bp"),
        ("routes.security_routes", "security_bp"),
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
            print(f"[BOOT][ERROR] failed to register blueprint: {module_name}.{blueprint_name}: {exc}")
            traceback.print_exc()


def register_context(app):
    @app.context_processor
    def inject_globals():
        role = session.get("role", "")
        user_id = session.get("user_id")
        display_name = session.get("display_name", "")
        try:
            from services.turnstile_service import turnstile_widget_enabled_for
        except Exception:
            def turnstile_widget_enabled_for(action):
                return False
        return {
            "APP_NAME": app.config.get("APP_NAME", "FUMAP GO"),
            "current_role": role,
            "current_user_id": user_id,
            "current_display_name": display_name,
            "is_logged_in": bool(user_id),
            "is_admin": role == "ADMIN_OPERATOR",
            "request_path": request.path,
            "turnstile_site_key": app.config.get("TURNSTILE_SITE_KEY", ""),
            "turnstile_enabled_for": turnstile_widget_enabled_for,
            "register_invite_required": app.config.get("REGISTER_REQUIRE_INVITE_CODE", False),
        }

    @app.template_filter("twd")
    def twd(value):
        try:
            amount = int(value or 0)
        except Exception:
            amount = 0
        return f"{amount:,} TWD"


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

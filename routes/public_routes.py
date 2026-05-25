from flask import Blueprint, render_template, current_app, jsonify, request, session

from db import get_db
from services.order_service import list_public_stores


public_bp = Blueprint("public", __name__)


@public_bp.get("/")
def home():
    return render_template("mobile/home.html")


@public_bp.get("/show")
def show():
    db = get_db()
    city_block = request.args.get("city_block", "").strip().upper() or None
    stores = list_public_stores(db, city_block=city_block)

    return render_template(
        "mobile/customer/marketplace.html",
        stores=stores,
        selected_city_block=city_block or "",
    )


@public_bp.get("/health")
def health():
    try:
        db = get_db()
        db.execute("SELECT 1").fetchone()

        return jsonify(
            {
                "ok": True,
                "app": current_app.config.get("APP_NAME", "FUMAP GO"),
                "database": "ok",
                "mode": "commercial-v1",
            }
        )

    except Exception as e:
        return jsonify(
            {
                "ok": False,
                "error": str(e),
            }
        ), 500
        
        

@public_bp.route("/v2/events/poll", methods=["GET", "HEAD"])
def v2_events_poll_compat():
    """
    Backward-compatible realtime poll endpoint.

    Purpose:
    - Stop old UptimeRobot / old frontend from producing 404.
    - Keep production stable while current frontend uses:
      /store/realtime/status
      /driver/realtime/status

    This endpoint intentionally returns 200 for unauthenticated monitors.
    """
    role = (request.args.get("role") or session.get("role") or "").strip().upper()
    after_id_raw = request.args.get("after_id", "0")

    try:
        after_id = int(after_id_raw or 0)
    except Exception:
        after_id = 0

    base_payload = {
        "ok": True,
        "compat": True,
        "endpoint": "/v2/events/poll",
        "role": role or "GUEST",
        "after_id": after_id,
        "events": [],
        "should_ring": False,
        "message": "",
        "target_url": "/",
    }

    # UptimeRobot / external HEAD request: no session, no auth.
    # Return 200 to prevent noisy 404 logs.
    if not session.get("user_id") and session.get("role") != "ADMIN_OPERATOR":
        base_payload.update(
            {
                "authenticated": False,
                "note": "Compatibility endpoint. Current frontend uses /store/realtime/status or /driver/realtime/status.",
            }
        )
        return jsonify(base_payload)

    # STORE compatibility: reuse existing store realtime payload.
    if role == "STORE" and session.get("role") == "STORE":
        try:
            from services.permission_service import get_current_store
            from routes.store_routes import (
                _store_is_active,
                _store_inactive_realtime_payload,
                _store_realtime_payload,
            )

            db = get_db()
            store = get_current_store()

            if not store:
                base_payload.update(
                    {
                        "ok": False,
                        "authenticated": True,
                        "error": "STORE_NOT_FOUND",
                        "target_url": "/login",
                    }
                )
                return jsonify(base_payload)

            if not _store_is_active(store):
                payload = _store_inactive_realtime_payload(store)
            else:
                payload = _store_realtime_payload(db, store)

            payload.update(
                {
                    "compat": True,
                    "endpoint": "/v2/events/poll",
                    "after_id": after_id,
                    "events": [],
                    "authenticated": True,
                }
            )
            return jsonify(payload)

        except Exception as exc:
            base_payload.update(
                {
                    "ok": False,
                    "authenticated": True,
                    "error": str(exc),
                    "target_url": "/store",
                }
            )
            return jsonify(base_payload)

    # DRIVER compatibility placeholder.
    if role == "DRIVER" and session.get("role") == "DRIVER":
        base_payload.update(
            {
                "authenticated": True,
                "target_url": "/driver",
                "note": "Driver realtime uses /driver/realtime/status.",
            }
        )
        return jsonify(base_payload)

    base_payload.update(
        {
            "authenticated": True,
            "target_url": "/",
            "note": "No matching realtime role for current session.",
        }
    )
    return jsonify(base_payload)
from flask import Blueprint, render_template, current_app, jsonify, request

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

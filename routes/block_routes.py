from flask import Blueprint, render_template, request, redirect, flash

from db import get_db
from services.permission_service import admin_required


block_bp = Blueprint("block", __name__)


@block_bp.get("/blocks")
@admin_required
def blocks_timeline():
    db = get_db()

    rows = db.execute(
        """
        SELECT *
        FROM blocks
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()

    return render_template(
        "mobile/block/timeline.html",
        blocks=rows,
        order_code="",
        title="BlockFGO 證據時間線",
    )


@block_bp.get("/blocks/order/<order_code>")
@admin_required
def order_blocks(order_code):
    db = get_db()
    order_code = (order_code or "").strip().upper()

    order = db.execute(
        """
        SELECT o.*,
               s.store_code,
               s.store_name,
               d.driver_code,
               d.driver_name
        FROM orders o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN drivers d ON d.id = o.driver_id
        WHERE o.order_code = ?
        LIMIT 1
        """,
        (order_code,),
    ).fetchone()

    if not order:
        flash("找不到訂單。", "danger")
        return redirect("/blocks")

    blocks = db.execute(
        """
        SELECT *
        FROM blocks
        WHERE order_code = ?
        ORDER BY id ASC
        """,
        (order_code,),
    ).fetchall()

    return render_template(
        "mobile/block/timeline.html",
        blocks=blocks,
        order=order,
        order_code=order_code,
        title=f"{order_code} 證據時間線",
    )

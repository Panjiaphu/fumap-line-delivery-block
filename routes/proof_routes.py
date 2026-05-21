from flask import Blueprint, abort, send_file

from db import get_db
from services.permission_service import login_required, current_user
from services.proof_service import (
    ALLOWED_PROOF_TYPES,
    ProofAccessError,
    ProofNotFoundError,
    can_view_order_proof,
    get_order_for_proof,
    local_path_from_upload_url,
    proof_url_for_type,
)


proof_bp = Blueprint("proof", __name__)


@proof_bp.get("/proofs/orders/<order_code>/<proof_type>")
@login_required
def view_order_proof(order_code, proof_type):
    db = get_db()
    user = current_user()

    proof_type = (proof_type or "").strip().lower()

    if proof_type not in ALLOWED_PROOF_TYPES:
        abort(404)

    order = get_order_for_proof(db, order_code)

    if not order:
        abort(404)

    if not can_view_order_proof(user, order, proof_type):
        abort(403)

    proof_url = proof_url_for_type(order, proof_type)

    if not proof_url:
        abort(404)

    try:
        local_path = local_path_from_upload_url(proof_url)

    except ProofAccessError:
        abort(403)

    except ProofNotFoundError:
        abort(404)

    return send_file(
        local_path,
        conditional=True,
        max_age=0,
    )

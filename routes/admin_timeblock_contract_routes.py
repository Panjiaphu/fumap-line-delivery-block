from flask import Blueprint, jsonify

from services.permission_service import admin_required
from services.timeblock_contract_service import contract_health


admin_timeblock_contract_bp = Blueprint(
    "admin_timeblock_contract",
    __name__,
    url_prefix="/admin/timeblock",
)


@admin_timeblock_contract_bp.get("/contracts")
@admin_required
def timeblock_contracts():
    return jsonify(contract_health())

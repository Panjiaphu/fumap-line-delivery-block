SUPPORTED_CONTRACT_VERSIONS = {"v1"}
DEFAULT_CONTRACT_VERSION = "v1"

EVENT_CONTRACTS = {
    "STORE_ONLINE_REWARD_FINALIZED": "v1",
    "SHIPPER_ONLINE_REWARD_FINALIZED": "v1",
    "CUSTOMER_ORDER_CREATED": "v1",
    "CUSTOMER_ORDER_COMPLETED": "v1",
    "CUSTOMER_BONUS_GRANTED": "v1",
    "ADMIN_REWARD_REVIEWED": "v1",
}


def normalize_contract_version(value=""):
    value = (value or DEFAULT_CONTRACT_VERSION).strip().lower()
    return value if value in SUPPORTED_CONTRACT_VERSIONS else ""


def contract_for_event(event_code):
    code = (event_code or "").strip().upper()
    return EVENT_CONTRACTS.get(code, DEFAULT_CONTRACT_VERSION)


def apply_contract_version(event):
    event = dict(event or {})
    payload = dict(event.get("payload") or {})
    version = normalize_contract_version(payload.get("contract_version"))
    if not version:
        version = contract_for_event(event.get("event_code"))
    payload["contract_version"] = version
    event["payload"] = payload
    event["contract_version"] = version
    return event


def contract_health():
    return {
        "ok": True,
        "default_contract_version": DEFAULT_CONTRACT_VERSION,
        "supported_contract_versions": sorted(SUPPORTED_CONTRACT_VERSIONS),
        "event_contracts": EVENT_CONTRACTS,
    }

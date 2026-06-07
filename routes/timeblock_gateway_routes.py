import json
import os
import urllib.error
import urllib.request

from flask import Blueprint, jsonify, request, session

from db import get_db
from services.timeblock_gateway_service import (
    forward_event_to_timeblock,
    get_outbound_event,
    integration_health,
    record_outbound_event,
    timeblock_config,
    validate_event_payload,
)


timeblock_gateway_bp = Blueprint(
    "timeblock_gateway",
    __name__,
    url_prefix="/api/projects/fumapgo",
)


def _env(key, default=""):
    return (os.getenv(key, default) or "").strip()


def _bearer_token():
    header = request.headers.get("Authorization", "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _admin_session_allowed():
    return session.get("role") == "ADMIN_OPERATOR"


def _configured_tokens():
    tokens = []
    for key in ("TIMEBLOCK_PROJECT_TOKEN", "FUMAPGO_AGENT_TOKEN", "ADMIN_TOKEN"):
        value = _env(key, "")
        if value:
            tokens.append(value)
    return set(tokens)


def _require_gateway_auth():
    """
    Gateway endpoints accept either an authenticated Admin session or a configured Bearer token.

    This avoids public reward writes while still allowing an AI/admin agent or internal job to call the API.
    """
    if _admin_session_allowed():
        return None

    token = _bearer_token()
    tokens = _configured_tokens()

    if not tokens:
        return jsonify(
            {
                "ok": False,
                "error": "gateway token is not configured",
                "required_env": ["TIMEBLOCK_PROJECT_TOKEN", "FUMAPGO_AGENT_TOKEN", "ADMIN_TOKEN"],
            }
        ), 503

    if token not in tokens:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    return None


def _proxy_get_json(url):
    cfg = timeblock_config()
    token = _env("TIMEBLOCK_PROJECT_TOKEN", "")

    if not cfg["api_base_url"] or not token:
        return {
            "ok": False,
            "proxied": False,
            "error": "TIMEBLOCK_API_BASE_URL or TIMEBLOCK_PROJECT_TOKEN is not configured",
            "fallback": {
                "timeblock_webapp_url": cfg["webapp_url"],
                "source_project": cfg["source_project"],
                "project_code": cfg["project_code"],
            },
        }, 503

    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
            try:
                data = json.loads(raw or "{}")
            except Exception:
                data = {"raw": raw}
            return {"ok": True, "proxied": True, "data": data}, 200

    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "proxied": True, "error": f"HTTP {exc.code}: {error_body}"}, exc.code

    except Exception as exc:
        return {"ok": False, "proxied": True, "error": str(exc)}, 502


@timeblock_gateway_bp.get("/integration-health")
def api_integration_health():
    auth_error = _require_gateway_auth()
    if auth_error:
        return auth_error

    db = get_db()
    return jsonify(integration_health(db))


@timeblock_gateway_bp.post("/events")
def api_create_timeblock_event():
    auth_error = _require_gateway_auth()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    valid, errors, event = validate_event_payload(data)

    if not valid:
        return jsonify({"ok": False, "errors": errors}), 400

    db = get_db()
    row, created = record_outbound_event(db, event)

    forward_result = {"ok": False, "queued": True, "forwarded": False, "skipped": True}

    if created:
        forward_result = forward_event_to_timeblock(db, row)
        row = get_outbound_event(db, row["id"])

    return jsonify(
        {
            "ok": True,
            "created": created,
            "duplicate": not created,
            "queued": True,
            "forward_result": forward_result,
            "event": {
                "id": row["id"],
                "source_project": row["source_project"],
                "event_code": row["event_code"],
                "external_event_id": row["external_event_id"],
                "idempotency_key": row["idempotency_key"],
                "actor_role": row["actor_role"],
                "actor_external_id": row["actor_external_id"],
                "points_delta": int(row["points_delta"] or 0),
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "forwarded_at": row["forwarded_at"],
            },
        }
    ), 201 if created else 200


@timeblock_gateway_bp.get("/wallets/<path:external_user_id>/summary")
def api_wallet_summary(external_user_id):
    auth_error = _require_gateway_auth()
    if auth_error:
        return auth_error

    cfg = timeblock_config()
    if not cfg["api_base_url"]:
        return jsonify(
            {
                "ok": False,
                "proxied": False,
                "error": "TIMEBLOCK_API_BASE_URL is not configured",
                "fallback": {
                    "external_user_id": external_user_id,
                    "source_project": cfg["source_project"],
                    "project_code": cfg["project_code"],
                    "timeblock_webapp_url": cfg["webapp_url"],
                },
            }
        ), 503

    url = f"{cfg['api_base_url']}/api/projects/{cfg['project_code']}/wallets/{external_user_id}/summary"
    data, status = _proxy_get_json(url)
    return jsonify(data), status


@timeblock_gateway_bp.get("/wallets/<path:external_user_id>/blocks")
def api_wallet_blocks(external_user_id):
    auth_error = _require_gateway_auth()
    if auth_error:
        return auth_error

    cfg = timeblock_config()
    if not cfg["api_base_url"]:
        return jsonify(
            {
                "ok": False,
                "proxied": False,
                "error": "TIMEBLOCK_API_BASE_URL is not configured",
                "fallback": {
                    "external_user_id": external_user_id,
                    "source_project": cfg["source_project"],
                    "project_code": cfg["project_code"],
                    "timeblock_webapp_url": cfg["webapp_url"],
                },
            }
        ), 503

    url = f"{cfg['api_base_url']}/api/projects/{cfg['project_code']}/wallets/{external_user_id}/blocks"
    data, status = _proxy_get_json(url)
    return jsonify(data), status

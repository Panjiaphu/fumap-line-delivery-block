import requests
from flask import current_app, request


TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


def _bool_config(key, default=False):
    return bool(current_app.config.get(key, default))


def turnstile_site_key():
    return (current_app.config.get("TURNSTILE_SITE_KEY") or "").strip()


def turnstile_secret_key():
    return (current_app.config.get("TURNSTILE_SECRET_KEY") or "").strip()


def turnstile_configured():
    return bool(turnstile_site_key() and turnstile_secret_key())


def turnstile_enabled_for(action):
    action = (action or "").strip().lower()

    flag_by_action = {
        "register": "REGISTER_TURNSTILE_ENABLED",
        "login": "LOGIN_TURNSTILE_ENABLED",
        "resend": "VERIFY_RESEND_TURNSTILE_ENABLED",
        "checkout": "CHECKOUT_TURNSTILE_ENABLED",
    }

    flag_name = flag_by_action.get(action, "TURNSTILE_ENABLED")

    if not _bool_config(flag_name, False):
        return False

    return turnstile_configured()


def turnstile_widget_enabled_for(action):
    return turnstile_enabled_for(action)


def verify_turnstile_response(token, *, remote_ip="", expected_action=""):
    secret = turnstile_secret_key()

    if not turnstile_configured():
        return True, ""

    token = (token or "").strip()

    if not token:
        return False, "Missing Turnstile token"

    payload = {
        "secret": secret,
        "response": token,
    }

    if remote_ip:
        payload["remoteip"] = remote_ip

    try:
        response = requests.post(TURNSTILE_VERIFY_URL, data=payload, timeout=6)
        data = response.json()
    except Exception as exc:
        return False, f"Turnstile verify failed: {exc}"

    if not data.get("success"):
        errors = data.get("error-codes") or []
        return False, ", ".join(str(item) for item in errors) or "Turnstile rejected"

    actual_action = (data.get("action") or "").strip()
    expected_action = (expected_action or "").strip()

    if expected_action and actual_action and actual_action != expected_action:
        return False, "Turnstile action mismatch"

    return True, ""


def verify_turnstile_request(action, *, remote_ip=""):
    if not turnstile_enabled_for(action):
        return True, ""

    token = request.form.get("cf-turnstile-response", "")
    return verify_turnstile_response(
        token,
        remote_ip=remote_ip,
        expected_action=action,
    )

from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, session

from services.abuse_guard import get_client_ip
from services.turnstile_service import verify_turnstile_request


security_bp = Blueprint("security", __name__, url_prefix="/security")


def _safe_next(default="/"):
    next_url = request.args.get("next") or request.form.get("next") or default

    if not next_url.startswith("/") or next_url.startswith("//"):
        return default

    return next_url


@security_bp.get("/turnstile/<action>")
def turnstile_challenge_page(action):
    action = (action or "").strip().lower()

    if action != "checkout":
        flash("安全驗證類型不支援。", "warning")
        return redirect("/")

    return render_template(
        "mobile/security/turnstile_challenge.html",
        action=action,
        next_url=_safe_next("/show"),
    )


@security_bp.post("/turnstile/<action>")
def turnstile_challenge_submit(action):
    action = (action or "").strip().lower()
    next_url = _safe_next("/show")

    if action != "checkout":
        flash("安全驗證類型不支援。", "warning")
        return redirect("/")

    ok, _message = verify_turnstile_request(action, remote_ip=get_client_ip())

    if not ok:
        flash("請完成安全驗證後再繼續。", "danger")
        return redirect(f"/security/turnstile/{action}?next={next_url}")

    session[f"turnstile_{action}_verified_at"] = datetime.now().isoformat(timespec="seconds")
    flash("安全驗證完成，請重新送出表單。", "success")
    return redirect(next_url)

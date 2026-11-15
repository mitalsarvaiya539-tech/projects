"""decorators.py - protects pages so only a logged-in admin can open them."""
from functools import wraps

from flask import g, jsonify, redirect, request, session, url_for

from database.db import db
from database.models import Admin


def _wants_json() -> bool:
    return (request.headers.get("X-Requested-With") == "fetch"
            or request.is_json or "application/json" in request.headers.get("Accept", ""))


def require_admin():
    """
    Runs before every protected request (blueprint.before_request).
    Returns None when the admin is logged in, otherwise a redirect / 401 response.
    """
    admin_id = session.get("admin_id")
    admin = db.session.get(Admin, admin_id) if admin_id else None
    if admin is None or not admin.is_active:
        session.clear()
        if _wants_json():
            return jsonify(ok=False, error="Your session expired. Please log in again."), 401
        target = request.full_path.rstrip("?") if request.method == "GET" else None
        return redirect(url_for("auth.login", next=target))
    g.admin = admin
    return None


def login_required(view):
    """Decorator version for single routes inside otherwise-public blueprints."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        blocked = require_admin()
        return blocked if blocked is not None else view(*args, **kwargs)
    return wrapper

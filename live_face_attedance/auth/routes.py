"""routes.py (auth) - /login, /logout and the admin profile page."""
import time
from functools import lru_cache

from flask import (Blueprint, current_app, flash, g, redirect, render_template, request,
                   session, url_for)
from sqlalchemy import func, or_, select
from werkzeug.security import check_password_hash, generate_password_hash

from database.db import db
from database.models import Admin
from utils import ServiceError, is_safe_redirect

from . import services
from .decorators import login_required

auth_bp = Blueprint("auth", __name__)

_failures: dict = {}          # (ip, username) -> [count, first_failure_time]


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return generate_password_hash("not-a-real-password")


def _is_locked(key) -> bool:
    entry = _failures.get(key)
    if not entry:
        return False
    if time.time() - entry[1] > current_app.config["LOGIN_LOCK_SECONDS"]:
        _failures.pop(key, None)
        return False
    return entry[0] >= current_app.config["LOGIN_MAX_ATTEMPTS"]


def _record_failure(key) -> None:
    entry = _failures.setdefault(key, [0, time.time()])
    entry[0] += 1


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_id"):
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        identifier = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not identifier or not password:
            flash("Please enter your username/email and password.", "danger")
        else:
            key = (request.remote_addr, identifier.lower())
            if _is_locked(key):
                flash("Too many failed attempts. Please wait a few minutes and try again.",
                      "danger")
            else:
                admin = db.session.scalar(select(Admin).where(or_(
                    func.lower(Admin.username) == identifier.lower(),
                    func.lower(Admin.email) == identifier.lower())))
                if admin is not None and admin.is_active and admin.check_password(password):
                    _failures.pop(key, None)
                    session.clear()                 # new session id after login
                    session["admin_id"] = admin.id
                    session.permanent = True
                    nxt = request.args.get("next")
                    return redirect(nxt if is_safe_redirect(nxt) else url_for("dashboard.index"))
                if admin is None:                   # keep timing similar for unknown users
                    check_password_hash(_dummy_hash(), password)
                _record_failure(key)
                flash("Invalid username/email or password.", "danger")
    return render_template("login.html")


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        try:
            services.change_password(g.admin, request.form.get("current_password", ""),
                                     request.form.get("new_password", ""),
                                     request.form.get("confirm_password", ""))
            flash("Password changed successfully.", "success")
            return redirect(url_for("auth.profile"))
        except ServiceError as exc:
            flash(str(exc), "danger")
    return render_template("profile.html", admin=g.admin)

"""services.py (auth) - admin creation and password rules."""
import re

from sqlalchemy import func, select

from database.db import db
from database.models import Admin
from utils import ServiceError

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise ServiceError("Password must be at least 8 characters long.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ServiceError("Password must contain at least one letter and one number.")


def create_admin(username: str, email: str, password: str) -> Admin:
    username, email = (username or "").strip(), (email or "").strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", username):
        raise ServiceError("Username: 3-50 characters (letters, numbers, . _ -).")
    if not EMAIL_RE.match(email):
        raise ServiceError("Enter a valid email address.")
    validate_password(password)
    taken = db.session.scalar(select(Admin.id).where(
        (func.lower(Admin.username) == username.lower()) | (func.lower(Admin.email) == email)))
    if taken:
        raise ServiceError("An admin with this username or email already exists.")
    admin = Admin(username=username, email=email)
    admin.set_password(password)
    db.session.add(admin)
    db.session.commit()
    return admin


def change_password(admin: Admin, current: str, new: str, confirm: str) -> None:
    if not admin.check_password(current):
        raise ServiceError("Current password is incorrect.")
    if new != confirm:
        raise ServiceError("New password and confirmation do not match.")
    validate_password(new)
    admin.set_password(new)
    db.session.commit()

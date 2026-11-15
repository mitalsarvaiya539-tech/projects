"""routes.py (dashboard) - dashboard page + JSON used by Chart.js."""
from flask import Blueprint, jsonify, render_template

from attendance import services
from auth.decorators import require_admin
from face_engine.camera import camera_manager

dashboard_bp = Blueprint("dashboard", __name__)
dashboard_bp.before_request(require_admin)


def _payload() -> dict:
    data = services.dashboard_data()
    data["live"]["camera"] = (f"Running ({camera_manager.mode})" if camera_manager.running
                              else "Idle")
    return data


@dashboard_bp.route("/")
@dashboard_bp.route("/dashboard")
def index():
    return render_template("dashboard.html", data=_payload())


@dashboard_bp.route("/dashboard/data")
def data():
    return jsonify(_payload())

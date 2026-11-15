"""routes.py (attendance) - records page, exports and the live camera endpoints."""
import logging
from io import BytesIO

from flask import (Blueprint, Response, abort, current_app, flash, jsonify, redirect,
                   render_template, request, send_file, url_for)
from sqlalchemy.exc import SQLAlchemyError

from auth.decorators import require_admin
from face_engine.camera import CameraError, camera_manager, mjpeg_stream
from face_engine.detector import ModelNotFoundError
from reports import exporters
from utils import Page, ServiceError

from . import live, services

log = logging.getLogger(__name__)
attendance_bp = Blueprint("attendance", __name__, url_prefix="/attendance")
attendance_bp.before_request(require_admin)


# ---------------------------------------------------------------- records page
@attendance_bp.route("/")
def records():
    f = services.AttendanceFilters.from_args(request.args)
    for message in f.errors:
        flash(message, "warning")
    rows, page, notice = [], Page(1, current_app.config["PER_PAGE"], 0), None
    if not f.errors:
        try:
            rows, page, notice = services.search_attendance(f, current_app.config["PER_PAGE"])
        except ServiceError as exc:
            flash(str(exc), "warning")
    summary = None
    if f.date_on and not f.errors:
        summary = services.day_summary(f.date_on, f.course or None, f.semester,
                                       f.division or None)
    return render_template("attendance.html", rows=rows, page=page, f=f, notice=notice,
                           params=f.query_params(), options=services.filter_options(),
                           summary=summary)


@attendance_bp.route("/export/<fmt>")
def export(fmt):
    if fmt not in ("csv", "xlsx"):
        abort(404)
    f = services.AttendanceFilters.from_args(request.args)
    back = redirect(url_for("attendance.records", **f.query_params()))
    if f.errors:
        flash(f.errors[0], "warning")
        return back
    try:
        rows, _page, _notice = services.search_attendance(f, paginate=False)
        if not rows:
            flash("There are no records to export for the selected filters.", "warning")
            return back
        data, filename, mimetype = exporters.export_rows(rows, fmt)
    except ServiceError as exc:
        flash(str(exc), "warning")
        return back
    except Exception:
        log.exception("export failed")
        flash("Export failed. Please try again or narrow the filters.", "danger")
        return back
    return send_file(BytesIO(data), mimetype=mimetype, as_attachment=True, download_name=filename)


# ------------------------------------------------------------------ live camera
@attendance_bp.route("/live")
def live_page():
    running = camera_manager.running and camera_manager.mode == "attendance"
    return render_template("live_attendance.html", running=running,
                           default_session=current_app.config["DEFAULT_SESSION_LABEL"])


@attendance_bp.post("/live/start")
def live_start():
    data = request.get_json(silent=True) or {}
    try:
        label = services.validate_session_label(data.get("session_label"))
        _proc, registered = live.start_session(current_app._get_current_object(), label)
    except (ServiceError, CameraError, ModelNotFoundError) as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except SQLAlchemyError:
        log.exception("database error while starting live attendance")
        return jsonify(ok=False, error="Database error. Is MySQL running?"), 500
    except Exception:
        log.exception("could not start live attendance")
        return jsonify(ok=False, error="Could not start recognition. See the server log."), 500
    return jsonify(ok=True, registered_students=registered, session_label=label)


@attendance_bp.post("/live/stop")
def live_stop():
    if camera_manager.mode == "attendance":
        camera_manager.stop()
    return jsonify(ok=True)


@attendance_bp.get("/live/feed")
def live_feed():
    if not (camera_manager.running and camera_manager.mode == "attendance"):
        return Response("Camera is not running", status=409)
    resp = Response(mjpeg_stream(camera_manager),
                    mimetype="multipart/x-mixed-replace; boundary=frame")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@attendance_bp.get("/live/status")
def live_status():
    camera_manager.ping()
    proc = camera_manager.processor if camera_manager.mode == "attendance" else None
    if not (camera_manager.running and proc is not None):
        return jsonify(running=False, error=camera_manager.last_error)
    snap = proc.snapshot(request.args.get("after", 0, type=int))
    snap.update(running=True, fps=round(camera_manager.fps, 1), session_label=proc.session_label,
                camera_error=camera_manager.last_error)
    return jsonify(snap)

"""
live.py - live face-recognition attendance.

LiveAttendanceProcessor receives every webcam frame (from CameraManager) and:
  1. every N-th frame: detects faces, embeds them, matches them against the
     in-memory gallery (no database query per frame)
  2. requires the same student in several consecutive checks before marking
     (protects against one-frame mistakes)
  3. marks attendance ONCE per student per session (remembered in memory
     + protected again by the database UNIQUE constraint)
  4. draws boxes: green = marked, blue = already marked, amber = verifying,
     red = unknown face (never marked)
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime

from face_engine.camera import camera_manager
from face_engine.detector import get_detector
from face_engine.drawing import AMBER, BLUE, GREEN, RED, ascii_text, draw_face, draw_strip
from face_engine.encoder import ensure_models, get_encoder
from face_engine.recognizer import gallery, reload_gallery

from . import services

log = logging.getLogger(__name__)


class LiveAttendanceProcessor:
    def __init__(self, session_label, marker, logger, marked_loader, threshold=0.363,
                 confirm_frames=3, every_n=3, unknown_interval=10,
                 detector_factory=get_detector, encoder_factory=get_encoder,
                 gallery_obj=gallery, clock=time.time):
        self.session_label = session_label
        self.marker = marker                  # (student_pk, similarity, label) -> "marked"/"already_marked"
        self.logger = logger                  # (status, student_pk, similarity) -> None
        self.marked_loader = marked_loader    # () -> set of student pks already marked
        self.threshold = threshold
        self.confirm_frames = confirm_frames
        self.every_n = max(1, every_n)
        self.unknown_interval = unknown_interval
        self.detector_factory = detector_factory
        self.encoder_factory = encoder_factory
        self.gallery = gallery_obj
        self.clock = clock

        self.marked_ids: set[int] = set(marked_loader())
        self.session_marked = 0
        self.last_error: str | None = None
        self.events: deque = deque(maxlen=60)
        self._event_id = 0
        self._lock = threading.Lock()
        self._results: list[dict] = []
        self._frame_no = 0
        self._streak: dict[int, int] = {}
        self._unknown_streak = 0
        self._last_unknown_log = -1e9
        self._recent_marked: dict[int, float] = {}
        self._announced: set[int] = set()
        self._last_refresh = clock()

    # ------------------------------------------------------------ per frame
    def process(self, frame):
        self._frame_no += 1
        if (self._frame_no - 1) % self.every_n == 0:       # heavy work only every N-th frame
            try:
                results = self._analyze(frame)
                with self._lock:
                    self._results, self.last_error = results, None
            except Exception as exc:
                log.exception("recognition failed")
                with self._lock:
                    self._results, self.last_error = [], f"Recognition failed: {exc}"
        with self._lock:
            results = list(self._results)
        for r in results:
            self._draw(frame, r)
        draw_strip(frame, f"Session: {self.session_label} | Faces: {len(results)}")
        return frame

    def _analyze(self, frame) -> list[dict]:
        now = self.clock()
        if now - self._last_refresh > 60:                  # pick up a date change / other sessions
            self._last_refresh = now
            try:
                self.marked_ids = set(self.marked_loader())
            except Exception:
                log.exception("could not refresh marked list")

        faces = self.detector_factory().detect(frame)
        encoder = self.encoder_factory()
        matches = []
        for face in faces:
            pk, sim = self.gallery.match(encoder.encode(frame, face), self.threshold)
            matches.append((face, pk, sim))

        seen = {}
        for _f, pk, sim in matches:
            if pk is not None:
                seen[pk] = max(sim, seen.get(pk, 0.0))
        self._streak = {pk: self._streak.get(pk, 0) + 1 for pk in seen}   # consecutive sightings

        results, unknown_present = [], False
        for face, pk, sim in matches:
            if pk is None:
                unknown_present = True
                results.append({"box": face.box, "state": "unknown", "name": "Unknown Face",
                                "enrollment_no": "", "similarity": round(sim, 3)})
                continue
            enrollment_no, name = self.gallery.meta.get(pk, ("?", "?"))
            state = self._resolve(pk, sim, now, name, enrollment_no)
            results.append({"box": face.box, "state": state, "name": name,
                            "enrollment_no": enrollment_no, "similarity": round(sim, 3)})
        self._handle_unknown(unknown_present, now)
        return results

    def _resolve(self, pk, sim, now, name, enrollment_no) -> str:
        if pk in self.marked_ids:                          # already present in this session
            if self._streak.get(pk, 0) >= 2 and pk not in self._announced:
                self._announced.add(pk)
                self._add_event("already", name, enrollment_no, sim)
                self.logger("already_marked", pk, sim)
            return "marked" if now - self._recent_marked.get(pk, -1e9) < 4 else "already"
        if self._streak.get(pk, 0) < self.confirm_frames:
            return "verifying"
        try:
            outcome = self.marker(pk, sim, self.session_label)
        except Exception as exc:
            log.exception("could not mark attendance")
            self.last_error = f"Could not save attendance: {exc}"
            return "verifying"                             # retry on the next check
        self.marked_ids.add(pk)
        self._announced.add(pk)
        if outcome == "marked":
            self._recent_marked[pk] = now
            self.session_marked += 1
            self._add_event("marked", name, enrollment_no, sim)
            return "marked"
        self._add_event("already", name, enrollment_no, sim)
        return "already"

    def _handle_unknown(self, present: bool, now: float) -> None:
        self._unknown_streak = self._unknown_streak + 1 if present else 0
        if (self._unknown_streak >= self.confirm_frames
                and now - self._last_unknown_log >= self.unknown_interval):
            self._last_unknown_log = now
            self.logger("unknown", None, None)             # event only - no face data stored
            self._add_event("unknown", "Unknown Face", "", None)

    def _add_event(self, kind, name, enrollment_no, sim) -> None:
        with self._lock:
            self._event_id += 1
            self.events.append({"id": self._event_id, "type": kind, "name": name,
                                "enrollment_no": enrollment_no,
                                "time": datetime.now().strftime("%H:%M:%S"),
                                "similarity": None if sim is None else round(sim, 3)})

    # -------------------------------------------------------------- drawing
    @staticmethod
    def _draw(frame, r) -> None:
        state, sim = r["state"], r["similarity"]
        if state == "unknown":
            draw_face(frame, r["box"], ["Unknown Face", "Status: Not Registered",
                                        "Attendance: Not Marked"], RED)
            return
        lines = [ascii_text(r["name"]), f"ID: {ascii_text(r['enrollment_no'])}"]
        if state == "verifying":
            draw_face(frame, r["box"], lines + [f"Verifying... ({sim:.2f})"], AMBER)
        elif state == "marked":
            draw_face(frame, r["box"], lines + [f"Recognized ({sim:.2f})", "Attendance marked"], GREEN)
        else:
            draw_face(frame, r["box"], lines + [f"Recognized ({sim:.2f})",
                                                "Attendance already marked"], BLUE)

    # --------------------------------------------------------------- status
    def snapshot(self, after_id: int = 0) -> dict:
        with self._lock:
            return {"faces": [{k: v for k, v in r.items() if k != "box"} for r in self._results],
                    "events": [e for e in self.events if e["id"] > after_id],
                    "session_marked": self.session_marked, "error": self.last_error,
                    "registered_students": len(self.gallery)}


def start_session(app, session_label: str):
    """Load models + gallery, open the camera and start recognising."""
    ensure_models()                                        # clear error if model files are missing
    registered = reload_gallery()

    def marker(pk, similarity, label):
        with app.app_context():
            return services.mark_attendance(pk, similarity, label).status

    def logger(status, pk, similarity):
        with app.app_context():
            services.log_recognition(status, pk, similarity)

    def loader():
        with app.app_context():
            return services.marked_student_ids(services.today(), session_label)

    cfg = app.config
    processor = LiveAttendanceProcessor(
        session_label, marker, logger, loader, threshold=cfg["RECOGNITION_THRESHOLD"],
        confirm_frames=cfg["CONFIRM_FRAMES"], every_n=cfg["PROCESS_EVERY_N_FRAMES"],
        unknown_interval=cfg["UNKNOWN_LOG_INTERVAL"])
    camera_manager.start("attendance", processor)
    return processor, registered

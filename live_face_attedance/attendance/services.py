"""
services.py (attendance) - all attendance database logic.

Contains:
  * marking attendance (with duplicate prevention)
  * recognition log helper
  * searching / filtering attendance records
  * daily, weekly, monthly reports and dashboard data (using calculations.py)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from database.db import db
from database.models import Attendance, RecognitionLog, Student
from utils import (SAFE_LABEL, Page, ServiceError, clamp_page, escape_like, parse_date)

from . import calculations as calc

log = logging.getLogger(__name__)
MAX_RANGE_DAYS = 92


# ============================================================================
# Time (always taken from the DATABASE server so every record uses one clock)
# ============================================================================
def today() -> date:
    return db.session.scalar(select(func.current_date()))


# ============================================================================
# Marking attendance
# ============================================================================
@dataclass
class MarkResult:
    status: str                    # "marked" | "already_marked"
    student: Student | None = None
    record: Attendance | None = None


def mark_attendance(student_pk: int, similarity: float | None = None,
                    session_label: str = "General") -> MarkResult:
    """
    Mark one student present for TODAY in the given session.

    Duplicate protection has TWO layers:
      1. this function checks for an existing row first (friendly, fast)
      2. the UNIQUE(student_id, attendance_date, session_label) constraint in MySQL
         rejects a duplicate even if two requests race each other
    """
    student = db.session.get(Student, student_pk)
    if student is None or student.status != "active":
        raise ServiceError("Student not found or not active.")
    day = today()

    existing = db.session.scalar(
        select(Attendance).where(Attendance.student_id == student_pk,
                                 Attendance.attendance_date == day,
                                 Attendance.session_label == session_label))
    if existing is not None:
        log_recognition("already_marked", student_pk, similarity)
        return MarkResult("already_marked", student, existing)

    record = Attendance(student_id=student_pk, session_label=session_label,
                        attendance_date=day, attendance_time=func.current_time(),
                        status="Present", similarity=similarity)
    db.session.add(record)
    db.session.add(RecognitionLog(student_id=student_pk, recognition_status="recognized",
                                  similarity=similarity))
    try:
        db.session.commit()
    except IntegrityError:                    # someone else inserted it a moment ago
        db.session.rollback()
        log_recognition("already_marked", student_pk, similarity)
        return MarkResult("already_marked", student, None)
    return MarkResult("marked", student, record)


def log_recognition(status: str, student_pk: int | None = None,
                    similarity: float | None = None) -> None:
    """Write one row to recognition_logs. Never raises (must not break live video)."""
    try:
        db.session.add(RecognitionLog(student_id=student_pk, recognition_status=status,
                                      similarity=similarity))
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception("could not write recognition log")


def marked_student_ids(day: date, session_label: str) -> set[int]:
    rows = db.session.scalars(
        select(Attendance.student_id).where(Attendance.attendance_date == day,
                                            Attendance.session_label == session_label,
                                            Attendance.status == "Present"))
    return set(rows)


def validate_session_label(label: str) -> str:
    label = (label or "").strip() or "General"
    if not SAFE_LABEL.match(label):
        raise ServiceError("Session name may contain letters, numbers, spaces, - and _ "
                           "(max 30 characters).")
    return label


# ============================================================================
# Data gathering for calculations
# ============================================================================
def _class_conditions(course=None, semester=None, division=None):
    conds = []
    if course:
        conds.append(Student.course == course)
    if semester:
        conds.append(Student.semester == semester)
    if division:
        conds.append(Student.division == division)
    return conds


def load_students(course=None, semester=None, division=None, student_pk=None,
                  extra_conditions=()) -> list[dict]:
    """ACTIVE students of a class as plain dicts (what calculations.py expects)."""
    stmt = select(Student).where(Student.status == "active",
                                 *_class_conditions(course, semester, division),
                                 *extra_conditions)
    if student_pk:
        stmt = stmt.where(Student.id == student_pk)
    stmt = stmt.order_by(Student.full_name)
    return [{"id": s.id, "enrollment_no": s.enrollment_no, "name": s.full_name,
             "course": s.course, "semester": s.semester, "division": s.division,
             "registered": s.registration_date} for s in db.session.scalars(stmt)]


def fetch_working_days(d1: date, d2: date, course=None, semester=None, division=None) -> set:
    stmt = (select(Attendance.attendance_date).join(Student, Attendance.student_id == Student.id)
            .where(Attendance.attendance_date.between(d1, d2), Attendance.status == "Present",
                   *_class_conditions(course, semester, division)).distinct())
    return set(db.session.scalars(stmt))


def fetch_present_pairs(d1: date, d2: date, student_ids: list[int]) -> set:
    if not student_ids:
        return set()
    stmt = (select(Attendance.student_id, Attendance.attendance_date)
            .where(Attendance.attendance_date.between(d1, d2), Attendance.status == "Present",
                   Attendance.student_id.in_(student_ids)).distinct())
    return {(sid, d) for sid, d in db.session.execute(stmt).all()}


# ============================================================================
# Reports
# ============================================================================
def report(d1: date, d2: date, course=None, semester=None, division=None,
           student_pk=None) -> dict:
    """Daily + student-wise + overall figures for a date range."""
    if d2 < d1:
        raise ServiceError("End date is before start date.")
    if (d2 - d1).days > 366:
        raise ServiceError("Please choose a range of at most one year.")
    if student_pk:                                    # class of that student defines working days
        one = db.session.get(Student, student_pk)
        if one is None:
            raise ServiceError("Student not found.")
        course, semester, division = one.course, one.semester, one.division
        students = load_students(student_pk=student_pk)
    else:
        students = load_students(course, semester, division)
    working = fetch_working_days(d1, d2, course, semester, division)
    pairs = fetch_present_pairs(d1, d2, [s["id"] for s in students])
    return calc.build_report(students, pairs, working, d1, d2)


def day_summary(day: date, course=None, semester=None, division=None) -> dict:
    students = load_students(course, semester, division)
    pairs = fetch_present_pairs(day, day, [s["id"] for s in students])
    return calc.day_summary(students, pairs, day)


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())      # Monday
    return start, start + timedelta(days=6)


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, nxt - timedelta(days=1)


def dashboard_data() -> dict:
    day = today()
    summary = day_summary(day)
    w1, w2 = week_bounds(day)
    weekly = report(w1, w2)
    m1, _ = month_bounds(day)
    monthly = report(m1, day)

    recent = db.session.execute(
        select(Attendance, Student).join(Student, Attendance.student_id == Student.id)
        .order_by(Attendance.id.desc()).limit(10)).all()

    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    unknown_today = db.session.scalar(
        select(func.count(RecognitionLog.id)).where(
            RecognitionLog.recognition_status == "unknown",
            RecognitionLog.recognized_at >= start, RecognitionLog.recognized_at < end))
    last_mark = db.session.scalar(
        select(func.max(Attendance.attendance_time)).where(Attendance.attendance_date == day))

    stats = [r for r in monthly["students"] if r["pct"] is not None][:8]
    return {
        "date": day.isoformat(),
        "cards": {"total": summary["total"], "present": summary["present"],
                  "absent": summary["absent"], "pct": summary["pct"] or 0},
        "live": {"unknown_today": unknown_today or 0,
                 "last_marked": last_mark.strftime("%H:%M:%S") if last_mark else None},
        "weekly": {"labels": [f"{d['label']} {d['date'].strftime('%d/%m')}" for d in weekly["daily"]],
                   "pct": [d["pct"] for d in weekly["daily"]]},
        "distribution": {"present": summary["present"], "absent": summary["absent"]},
        "recent": [{"enrollment_no": s.enrollment_no, "name": s.full_name,
                    "date": a.attendance_date.strftime("%d-%m-%Y"),
                    "time": a.attendance_time.strftime("%H:%M:%S"),
                    "status": a.status} for a, s in recent],
        "student_stats": [{"name": r["name"], "enrollment_no": r["enrollment_no"],
                           "pct": r["pct"], "present": r["present_days"],
                           "working": r["working_days"]} for r in stats],
    }


# ============================================================================
# Attendance search
# ============================================================================
@dataclass
class AttendanceFilters:
    enrollment_no: str = ""
    name: str = ""
    date_on: date | None = None
    date_from: date | None = None
    date_to: date | None = None
    course: str = ""
    semester: int | None = None
    division: str = ""
    status: str = ""
    page: int = 1
    errors: list = field(default_factory=list)

    @classmethod
    def from_args(cls, args) -> "AttendanceFilters":
        f = cls(enrollment_no=args.get("enrollment_no", "").strip(),
                name=args.get("name", "").strip(), course=args.get("course", "").strip(),
                division=args.get("division", "").strip(),
                status=args.get("status", "").strip())
        if f.status not in ("", "Present", "Absent"):
            f.status = ""
        for attr, key in (("date_on", "date"), ("date_from", "date_from"), ("date_to", "date_to")):
            try:
                setattr(f, attr, parse_date(args.get(key)))
            except ValueError:
                f.errors.append(f"Invalid date for '{key}'. Use the date picker.")
        sem = args.get("semester", "").strip()
        if sem:
            try:
                f.semester = int(sem)
            except ValueError:
                f.errors.append("Semester must be a number.")
        try:
            f.page = max(1, int(args.get("page", 1)))
        except ValueError:
            f.page = 1
        if f.date_from and f.date_to and f.date_to < f.date_from:
            f.errors.append("'To' date is before 'From' date.")
            f.date_from = f.date_to = None
        return f

    def scope(self):
        """(start, end) dates the user selected, or None when no date filter is used."""
        if self.date_on:
            return self.date_on, self.date_on
        if self.date_from or self.date_to:
            return (self.date_from or self.date_to), (self.date_to or today())
        return None

    def query_params(self) -> dict:
        """Filters as URL parameters (used by pagination and export links)."""
        raw = {"enrollment_no": self.enrollment_no, "name": self.name,
               "date": self.date_on.isoformat() if self.date_on else "",
               "date_from": self.date_from.isoformat() if self.date_from else "",
               "date_to": self.date_to.isoformat() if self.date_to else "",
               "course": self.course, "semester": self.semester or "",
               "division": self.division, "status": self.status}
        return {k: v for k, v in raw.items() if v != ""}


def _student_conditions(f: AttendanceFilters) -> list:
    conds = []
    if f.enrollment_no:
        conds.append(Student.enrollment_no.ilike(f"%{escape_like(f.enrollment_no)}%", escape="\\"))
    if f.name:
        conds.append(Student.full_name.ilike(f"%{escape_like(f.name)}%", escape="\\"))
    conds += _class_conditions(f.course, f.semester, f.division)
    return conds


def _row(att, stu, status=None, day=None, at=None) -> dict:
    return {"enrollment_no": stu.enrollment_no, "name": stu.full_name,
            "date": day or att.attendance_date, "time": at if att is None else att.attendance_time,
            "status": status or att.status, "course": stu.course,
            "semester": stu.semester, "division": stu.division}


def search_attendance(f: AttendanceFilters, per_page: int = 15, paginate: bool = True):
    """
    Returns (rows, Page, notice).

    * Status "Present" (or no status and no date)  -> read straight from the table.
    * Status "Absent", or no status WITH a date filter -> absentees are CALCULATED for
      the selected dates (active students with no attendance row).
    """
    scope = f.scope()
    want_absent = f.status == "Absent" or (f.status == "" and scope is not None)
    notice = None

    if f.status == "Absent" and scope is None:
        scope = (today(), today())
        notice = "No date selected, so today's absentees are shown."
    if want_absent and scope and (scope[1] - scope[0]).days > MAX_RANGE_DAYS:
        raise ServiceError(f"Absent/combined view supports at most {MAX_RANGE_DAYS} days. "
                           "Choose Status = Present or a shorter range.")

    conds = _student_conditions(f)
    present_rows = []
    if f.status != "Absent":
        base_where = [Attendance.status == "Present", *conds]
        if scope:
            base_where.append(Attendance.attendance_date.between(*scope))
        order = (Attendance.attendance_date.desc(), Attendance.attendance_time.desc(),
                 Attendance.id.desc())
        if not want_absent:                              # pure SQL pagination
            total = db.session.scalar(
                select(func.count(Attendance.id)).select_from(Attendance)
                .join(Student, Attendance.student_id == Student.id).where(*base_where))
            page = clamp_page(f.page, per_page if paginate else max(total, 1), total)
            stmt = (select(Attendance, Student).join(Student, Attendance.student_id == Student.id)
                    .where(*base_where).order_by(*order))
            if paginate:
                stmt = stmt.limit(page.per_page).offset(page.offset)
            return [_row(a, s) for a, s in db.session.execute(stmt).all()], page, notice
        stmt = (select(Attendance, Student).join(Student, Attendance.student_id == Student.id)
                .where(*base_where).order_by(*order))
        present_rows = [_row(a, s) for a, s in db.session.execute(stmt).all()]

    absent_rows = _absent_rows(f, scope, conds) if want_absent else []
    rows = present_rows + absent_rows
    rows.sort(key=lambda r: (r["date"], r["time"] or time.min, r["name"]), reverse=True)
    page = clamp_page(f.page, per_page if paginate else max(len(rows), 1), len(rows))
    if paginate:
        rows = rows[page.offset:page.offset + page.per_page]
    return rows, page, notice


def _absent_rows(f: AttendanceFilters, scope, conds) -> list[dict]:
    d1, d2 = scope
    students = db.session.scalars(
        select(Student).where(Student.status == "active", *conds)).all()
    if not students:
        return []
    working = fetch_working_days(d1, d2, f.course or None, f.semester, f.division or None)
    pairs = fetch_present_pairs(d1, d2, [s.id for s in students])
    if d1 == d2:
        days = [d1]                                       # an explicitly chosen day always counts
    else:
        days = [d for d in calc.daterange(d1, d2) if d in working or d == today()]
    rows = []
    for d in days:
        for s in students:
            if s.registration_date <= d and (s.id, d) not in pairs:
                rows.append(_row(None, s, status="Absent", day=d, at=None))
    return rows


def filter_options() -> dict:
    """Distinct values used by the filter drop-downs (real data from the students table)."""
    def distinct(col):
        return [v for v in db.session.scalars(select(col).distinct().order_by(col)) if v not in (None, "")]
    return {"courses": distinct(Student.course), "semesters": distinct(Student.semester),
            "divisions": distinct(Student.division)}

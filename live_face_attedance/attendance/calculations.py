"""
calculations.py - PURE attendance maths (no database, no Flask).

The database layer gathers plain data (students, who was present on which
day, which days were working days) and these functions calculate the numbers.
Keeping the maths separate makes it easy to test and to explain in a viva.

Key definitions
---------------
* Present : a row exists in the attendance table for that student/date.
* Absent  : an ACTIVE student, registered on/before that date, with no row.
            Absent students are calculated - they are never stored, so no fake
            records are created.
* Working day : a date on which at least one student of the selected class was
            marked present (so weekends/holidays do not lower percentages).
* Percentage  : present / (present + absent) * 100
"""
from __future__ import annotations

from datetime import date, timedelta


def daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def pct(part, whole):
    """Percentage rounded to 1 decimal, or None when there is nothing to divide by."""
    return round(part * 100.0 / whole, 1) if whole else None


def day_summary(students: list[dict], present_pairs: set, day: date) -> dict:
    """Total / present / absent / percentage for ONE date."""
    enrolled = [s for s in students if s["registered"] <= day]
    present = sum(1 for s in enrolled if (s["id"], day) in present_pairs)
    total = len(enrolled)
    return {"date": day, "total": total, "present": present,
            "absent": total - present, "pct": pct(present, total)}


def build_report(students: list[dict], present_pairs: set, working_days: set,
                 date_from: date, date_to: date) -> dict:
    """
    students      : [{"id", "enrollment_no", "name", "course", "semester",
                      "division", "registered": date}, ...]
    present_pairs : {(student_id, date), ...}
    working_days  : {date, ...}
    """
    days = daterange(date_from, date_to)
    work_sorted = sorted(d for d in days if d in working_days)

    daily, present_marks, possible_marks = [], 0, 0
    for d in days:
        if d in working_days:
            s = day_summary(students, present_pairs, d)
            present_marks += s["present"]
            possible_marks += s["total"]
            daily.append({**s, "label": d.strftime("%a"), "is_working": True})
        else:
            daily.append({"date": d, "label": d.strftime("%a"), "is_working": False,
                          "total": None, "present": None, "absent": None, "pct": None})

    rows = []
    for s in students:
        possible = [d for d in work_sorted if d >= s["registered"]]
        present_days = sum(1 for d in possible if (s["id"], d) in present_pairs)
        rows.append({**{k: s[k] for k in ("id", "enrollment_no", "name", "course",
                                          "semester", "division")},
                     "present_days": present_days, "working_days": len(possible),
                     "absent_days": len(possible) - present_days,
                     "pct": pct(present_days, len(possible))})
    rows.sort(key=lambda r: (r["pct"] is None, r["pct"] if r["pct"] is not None else 0,
                             r["name"].lower()))

    return {
        "date_from": date_from, "date_to": date_to, "daily": daily, "students": rows,
        "overall": {"working_days": len(work_sorted), "present_marks": present_marks,
                    "possible_marks": possible_marks, "pct": pct(present_marks, possible_marks)},
    }

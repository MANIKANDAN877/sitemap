from __future__ import annotations

import math
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

try:
    import mongomock
except Exception:  # pragma: no cover
    mongomock = None

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ABSENCE_STATUSES = {"ABSENT", "LEAVE"}
ALLOWED_STATUSES = PRESENT_STATUSES | ABSENCE_STATUSES


def _use_mock_mongo() -> bool:
    return os.getenv("USE_MOCK_MONGO", "0") == "1" and mongomock is not None


def _parse_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _safe_epoch_ms(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer epoch millisecond value")
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise ValueError(f"{field_name} is out of range")
    return value


def _utc_datetime(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, int) and not isinstance(value, bool):
        return _utc_datetime(value)
    return None


def _epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1000)


def _normalize_history_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _epoch_ms(value)
    if isinstance(value, list):
        return [_normalize_history_value(item) for item in value]
    if isinstance(value, dict):
        return {k: _normalize_history_value(v) for k, v in value.items()}
    return value


def _round_half_up(value: float | int | Decimal | None, digits: int) -> float | None:
    if value is None:
        return None
    q = Decimal("1").scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _build_shift_date(emp: dict[str, Any], punch_in: datetime) -> str:
    local = punch_in.astimezone(IST)
    shift_start = _parse_time(emp["shift_start"])
    shift_end = _parse_time(emp["shift_end"])
    if shift_end <= shift_start:
        return (local.date() - timedelta(days=1)).isoformat() if local.time() < shift_end else local.date().isoformat()
    return local.date().isoformat()


def _shift_end_for_date(date_str: str, shift_start: str, shift_end: str) -> datetime:
    day = date.fromisoformat(date_str)
    end_t = _parse_time(shift_end)
    if shift_end <= shift_start:
        day = day + timedelta(days=1)
    return datetime.combine(day, end_t, tzinfo=IST)


def _compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    start_local = punch_in.astimezone(IST)
    hour, minute = map(int, shift_start.split(":"))
    shift_start_dt = start_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    diff_seconds = (start_local - shift_start_dt).total_seconds()
    if diff_seconds <= 600:
        return 0
    return int(diff_seconds // 60)


def _compute_work_hours(punch_in: datetime, punch_out: datetime) -> float | None:
    if punch_in is None or punch_out is None:
        return None
    diff_seconds = (punch_out - punch_in).total_seconds()
    if diff_seconds <= 0:
        return 0.0
    value = Decimal(str(diff_seconds)) / Decimal("3600")
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _compute_overtime_minutes(punch_out: datetime, shift_end: str, shift_start: str, date_str: str) -> int:
    end_dt = _shift_end_for_date(date_str, shift_start, shift_end)
    diff_seconds = (punch_out.astimezone(IST) - end_dt).total_seconds()
    if diff_seconds <= 0:
        return 0
    minutes = math.floor(diff_seconds / 60)
    return minutes if minutes >= 30 else 0


def _derive_record_values(
    status: str,
    punch_in: datetime | None,
    punch_out: datetime | None,
    emp: dict[str, Any],
    date_str: str,
) -> tuple[int, float | None, int, bool]:
    late = 0
    if punch_in is not None and status in PRESENT_STATUSES:
        late = _compute_late_minutes(punch_in, emp["shift_start"])

    work_hours = None
    overtime_minutes = 0
    half_day = False
    if punch_in is not None and punch_out is not None and status in PRESENT_STATUSES:
        work_hours = _compute_work_hours(punch_in, punch_out)
        overtime_minutes = _compute_overtime_minutes(punch_out, emp["shift_end"], emp["shift_start"], date_str)
        half_day = bool(work_hours is not None and work_hours < 4.5)
    return late, work_hours, overtime_minutes, half_day


def _serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _epoch_ms(doc.get("created_at")),
    }


def _serialize_history_entry(entry: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for key, value in (entry.get("changes") or {}).items():
        if isinstance(value, dict):
            changes[key] = {
                "from": _normalize_history_value(value.get("from")),
                "to": _normalize_history_value(value.get("to")),
            }
    return {
        "at": _epoch_ms(entry.get("at")),
        "by": entry.get("by"),
        "reason": entry.get("reason"),
        "changes": changes,
    }


def _serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _epoch_ms(doc.get("punch_in")),
        "punch_out": _epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0)),
        "overtime_minutes": int(doc.get("overtime_minutes", 0)),
        "half_day": bool(doc.get("half_day", False)),
        "history": [_serialize_history_entry(h) for h in doc.get("history", [])],
    }


def _validate_epoch_ms(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer epoch millisecond value")
    if value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise ValueError(f"{field_name} is out of range")
    return value


def _date_range(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    start = date(y, m, 1).isoformat()
    if m == 12:
        end_year, end_month = y + 1, 1
    else:
        end_year, end_month = y, m + 1
    end = date(end_year, end_month, 1).isoformat()
    return start, end


def _month_last_day(month: str) -> date:
    y, m = map(int, month.split("-"))
    if m == 12:
        return date(y + 1, 1, 1) - timedelta(days=1)
    return date(y, m + 1, 1) - timedelta(days=1)


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: date

    @field_validator("emp_code")
    @classmethod
    def valid_emp_code(cls, value: str) -> str:
        if not value.startswith("EMP") or len(value) < 7:
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        suffix = value[3:]
        if not suffix.isdigit():
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        if len(suffix) < 4 or len(suffix) > 6:
            raise ValueError("emp_code must match EMP followed by 4-6 digits")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as exc:  # pragma: no cover
            raise ValueError("shift times must be HH:MM") from exc
        return value

    @field_validator("shift_start")
    @classmethod
    def ensure_shift_diff(cls, value: str) -> str:
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if "@" not in value or "." not in value:
            raise ValueError("email is invalid")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: int | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return _validate_epoch_ms(value, field_name="punched_at")


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: int | None = None

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return _validate_epoch_ms(value, field_name="punched_at")


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    punch_in: int | None = None
    punch_out: int | None = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in ALLOWED_STATUSES:
            raise ValueError("status must be one of the allowed values")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_punch(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return _validate_epoch_ms(value, field_name="punch_in")


if _use_mock_mongo():
    client = mongomock.MongoClient()
    db = client[os.getenv("MONGO_DB", "attendance_db")]
else:
    client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
    db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def startup() -> None:
    try:
        db.employees.create_index("emp_code", unique=True, name="emp_code_unique")
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="emp_date_unique")
        db.attendance_logs.create_index("emp_code", name="idx_attendance_emp_code")
        db.attendance_logs.create_index("date", name="idx_attendance_date")
        db.attendance_logs.create_index([("date", 1), ("emp_code", 1)], name="idx_attendance_date_emp")
    except Exception:
        pass


@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeCreate) -> dict[str, Any]:
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")
    if body.joined_on > date.today():
        raise HTTPException(status_code=422, detail="joined_on cannot be in the future")
    doc = body.model_dump()
    doc["joined_on"] = doc["joined_on"].isoformat()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        result = db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    created = db.employees.find_one({"_id": result.inserted_id})
    return _serialize_employee(created)


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    q: dict[str, Any] = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    return {"items": [_serialize_employee(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    punched_at = _utc_datetime(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    attendance_date = _build_shift_date(employee, punched_at)

    existing = db.attendance_logs.find_one({"emp_code": body.emp_code, "date": attendance_date})
    if existing:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    late_minutes = _compute_late_minutes(punched_at, employee["shift_start"])
    record = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    db.attendance_logs.insert_one(record)
    return _serialize_attendance(record)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    punched_at = _utc_datetime(body.punched_at) if body.punched_at is not None else datetime.now(timezone.utc)
    candidate = db.attendance_logs.find_one({
        "$and": [
            {"emp_code": body.emp_code},
            {"punch_in": {"$ne": None}},
            {"punch_in": {"$lte": punched_at}},
        ]
    }, sort=[("punch_in", -1)])
    if candidate is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record already punched out")
    if candidate["punch_in"] is None:
        raise HTTPException(status_code=422, detail="punch_in missing")
    if punched_at <= candidate["punch_in"]:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if (punched_at - candidate["punch_in"]).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="punch_out exceeds 24 hours from punch_in")

    late_minutes = candidate.get("late_minutes", 0)
    work_hours = _compute_work_hours(candidate["punch_in"], punched_at)
    overtime_minutes = _compute_overtime_minutes(punched_at, employee["shift_end"], employee["shift_start"], candidate["date"])
    half_day = bool(work_hours is not None and work_hours < 4.5)

    update = {
        "punch_out": punched_at,
        "work_hours": work_hours,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    db.attendance_logs.update_one({"_id": candidate["_id"]}, {"$set": update})
    updated = db.attendance_logs.find_one({"_id": candidate["_id"]})
    return _serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    items = list(db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [_serialize_attendance(item) for item in items], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    current_status = record.get("status")
    current_punch_in = record.get("punch_in")
    current_punch_out = record.get("punch_out")
    current_work_hours = record.get("work_hours")
    current_late_minutes = int(record.get("late_minutes", 0))
    current_overtime_minutes = int(record.get("overtime_minutes", 0))
    current_half_day = bool(record.get("half_day", False))

    new_status = body.status if body.status is not None else current_status
    new_punch_in = _coerce_datetime(body.punch_in) if body.punch_in is not None else current_punch_in
    new_punch_out = _coerce_datetime(body.punch_out) if body.punch_out is not None else current_punch_out

    if new_status in ABSENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None or new_punch_in is not None or new_punch_out is not None:
            raise HTTPException(status_code=422, detail="ABSENT and LEAVE records must not have punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        if new_punch_in is None:
            if current_punch_in is None:
                raise HTTPException(status_code=422, detail="presence status requires punch_in")
            new_punch_in = current_punch_in
        if new_punch_in is not None:
            if _build_shift_date(employee, new_punch_in) != date:
                raise HTTPException(status_code=422, detail="punch_in must stay on the attendance date")
        if new_punch_out is not None and new_punch_in is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    if new_punch_in is None and new_status not in ABSENCE_STATUSES:
        raise HTTPException(status_code=422, detail="presence status requires punch_in")

    updated = dict(record)
    updated["status"] = new_status
    updated["punch_in"] = new_punch_in
    updated["punch_out"] = new_punch_out
    late_minutes, work_hours, overtime_minutes, half_day = _derive_record_values(new_status, new_punch_in, new_punch_out, employee, date)
    updated["late_minutes"] = late_minutes
    updated["work_hours"] = work_hours
    updated["overtime_minutes"] = overtime_minutes
    updated["half_day"] = half_day

    changed_fields = {}
    for field_name, old_value, new_value in [
        ("status", current_status, new_status),
        ("punch_in", current_punch_in, new_punch_in),
        ("punch_out", current_punch_out, new_punch_out),
        ("work_hours", current_work_hours, work_hours),
        ("late_minutes", current_late_minutes, late_minutes),
        ("overtime_minutes", current_overtime_minutes, overtime_minutes),
        ("half_day", current_half_day, half_day),
    ]:
        if old_value != new_value:
            changed_fields[field_name] = {"from": _normalize_history_value(old_value), "to": _normalize_history_value(new_value)}

    if not changed_fields:
        raise HTTPException(status_code=422, detail="request changes nothing")

    updated["history"] = record.get("history", []) + [{
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changed_fields,
    }]

    db.attendance_logs.replace_one({"_id": record["_id"]}, updated)
    refreshed = db.attendance_logs.find_one({"_id": record["_id"]})
    return _serialize_attendance(refreshed)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="unknown employee")

    begin, end = _date_range(month)
    records = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": begin, "$lt": end}}))
    working_days = 0
    full_day = date.fromisoformat(begin)
    last_day = _month_last_day(month)
    while full_day <= last_day:
        if full_day.weekday() < 5 and full_day.isoformat() >= employee["joined_on"]:
            working_days += 1
        full_day += timedelta(days=1)

    present_days = 0.0
    leave_days = 0
    late_count = 0
    total_late_minutes = 0
    total_overtime_minutes = 0
    for rec in records:
        rec_date = date.fromisoformat(rec["date"])
        if rec_date.weekday() < 5 and rec_date.isoformat() >= employee["joined_on"]:
            if rec.get("status") in PRESENT_STATUSES:
                present_days += 0.5 if rec.get("half_day") else 1.0
        if rec.get("status") == "LEAVE":
            leave_days += 1
        if rec.get("late_minutes", 0) > 0:
            late_count += 1
            total_late_minutes += int(rec.get("late_minutes", 0))
        total_overtime_minutes += int(rec.get("overtime_minutes", 0))
    attendance_pct = _round_half_up((present_days / working_days) * 100, 4) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": _round_half_up(present_days, 4),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None,
) -> dict[str, Any]:
    begin, end = _date_range(month)
    employee_filter: dict[str, Any] = {}
    if department is not None:
        employee_filter["department"] = department
    employees = list(db.employees.find(employee_filter, {"_id": 0, "emp_code": 1, "department": 1, "joined_on": 1}))
    if not employees:
        return {"month": month, "items": []}
    emp_codes = [emp["emp_code"] for emp in employees]
    records = list(db.attendance_logs.find({"emp_code": {"$in": emp_codes}, "date": {"$gte": begin, "$lt": end}}, {"_id": 0}))
    employee_lookup = {emp["emp_code"]: emp for emp in employees}
    by_department: dict[str, dict[str, Any]] = {}
    for emp in employees:
        headcount = 0
        for day in range(( _month_last_day(month) - date.fromisoformat(begin) ).days + 1):
            current = date.fromisoformat(begin) + timedelta(days=day)
            if current.weekday() < 5 and current.isoformat() >= emp["joined_on"]:
                headcount += 1
        by_department.setdefault(emp["department"], {"department": emp["department"], "headcount": 0, "present_days": 0.0, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0, "work_hours_total": 0.0, "work_hours_count": 0})
        by_department[emp["department"]]["headcount"] += headcount

    for rec in records:
        dept = employee_lookup.get(rec["emp_code"], {}).get("department")
        if dept is None:
            continue
        bucket = by_department.setdefault(dept, {"department": dept, "headcount": 0, "present_days": 0.0, "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0, "work_hours_total": 0.0, "work_hours_count": 0})
        rec_date = date.fromisoformat(rec["date"])
        if rec_date.weekday() < 5 and rec_date.isoformat() >= employee_lookup[rec["emp_code"]]["joined_on"]:
            if rec.get("status") in PRESENT_STATUSES:
                bucket["present_days"] += 0.5 if rec.get("half_day") else 1.0
        if rec.get("status") == "LEAVE":
            bucket["leave_count"] += 1
        if rec.get("late_minutes", 0) > 0:
            bucket["late_count"] += 1
            bucket["total_late_minutes"] += int(rec.get("late_minutes", 0))
        if rec.get("status") == "ON_DUTY":
            bucket["on_duty_count"] += 1
        if rec.get("status") in PRESENT_STATUSES and rec.get("work_hours") is not None:
            bucket["work_hours_total"] += float(rec.get("work_hours"))
            bucket["work_hours_count"] += 1

    items = []
    for dept, info in sorted(by_department.items()):
        if info["headcount"] == 0:
            continue
        avg = _round_half_up((info["work_hours_total"] / info["work_hours_count"]) if info["work_hours_count"] else None, 2)
        items.append({
            "department": dept,
            "headcount": info["headcount"],
            "present_days": _round_half_up(info["present_days"], 4),
            "avg_work_hours": avg,
            "late_count": info["late_count"],
            "total_late_minutes": info["total_late_minutes"],
            "leave_count": info["leave_count"],
            "on_duty_count": info["on_duty_count"],
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: str | None = None,
) -> dict[str, Any]:
    begin, end = _date_range(month)
    employee_filter: dict[str, Any] = {}
    if department is not None:
        employee_filter["department"] = department
    employees = list(db.employees.find(employee_filter, {"_id": 0, "emp_code": 1, "name": 1, "department": 1}))
    employee_lookup = {emp["emp_code"]: emp for emp in employees}
    if not employee_lookup:
        return {"month": month, "items": []}
    records = list(db.attendance_logs.find({"emp_code": {"$in": list(employee_lookup)}, "date": {"$gte": begin, "$lt": end}}, {"_id": 0}))
    totals: dict[str, dict[str, Any]] = {}
    for emp_code in employee_lookup:
        totals[emp_code] = {"total_late_minutes": 0, "late_count": 0}
    for rec in records:
        total = rec.get("late_minutes", 0)
        if total > 0:
            totals.setdefault(rec["emp_code"], {"total_late_minutes": 0, "late_count": 0})
            totals[rec["emp_code"]]["total_late_minutes"] += int(total)
            totals[rec["emp_code"]]["late_count"] += 1
    ranking_rows = []
    for emp_code, row in totals.items():
        if row["total_late_minutes"] > 0:
            info = employee_lookup[emp_code]
            ranking_rows.append({
                "emp_code": emp_code,
                "name": info["name"],
                "department": info["department"],
                "total_late_minutes": row["total_late_minutes"],
                "late_count": row["late_count"],
            })
    ranking_rows.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))
    rows = []
    current_rank = 0
    previous_total = None
    for idx, row in enumerate(ranking_rows, start=1):
        if previous_total is None or row["total_late_minutes"] != previous_total:
            current_rank = idx
            previous_total = row["total_late_minutes"]
        if current_rank <= limit:
            rows.append({"rank": current_rank, **row})
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
) -> dict[str, Any]:
    if from_date > to_date:
        raise HTTPException(status_code=422, detail="from must be before or equal to to")
    delta = (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days + 1
    if delta > 92:
        raise HTTPException(status_code=422, detail="range must be 92 days or less")

    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(status_code=404, detail="unknown department")

    records = list(db.attendance_logs.find({"emp_code": {"$in": [emp["emp_code"] for emp in employees]}, "date": {"$gte": from_date, "$lte": to_date}}, {"_id": 0}))
    row_map: dict[str, dict[str, Any]] = {}
    current = date.fromisoformat(from_date)
    to_day = date.fromisoformat(to_date)
    while current <= to_day:
        row_map[current.isoformat()] = {
            "date": current.isoformat(),
            "is_working_day": current.weekday() < 5,
            "headcount": 0,
            "present_count": 0.0,
            "late_count": 0,
            "attendance_rate": None,
            "moving_avg_7d": None,
        }
        current += timedelta(days=1)

    for emp in employees:
        joined_on = date.fromisoformat(emp["joined_on"])
        current = date.fromisoformat(from_date)
        while current <= to_day:
            if joined_on <= current:
                row_map[current.isoformat()]["headcount"] += 1
            current += timedelta(days=1)

    for rec in records:
        rec_date = rec["date"]
        if rec_date not in row_map:
            continue
        row = row_map[rec_date]
        if rec.get("status") in PRESENT_STATUSES:
            row["present_count"] += 0.5 if rec.get("half_day") else 1.0
        if rec.get("late_minutes", 0) > 0:
            row["late_count"] += 1

    ordered = []
    for d in sorted(row_map):
        row = row_map[d]
        if row["is_working_day"] and row["headcount"] > 0:
            val = float(row["present_count"]) / float(row["headcount"])
            row["attendance_rate"] = _round_half_up(val, 4)
        elif row["is_working_day"]:
            row["attendance_rate"] = 0.0
        ordered.append(row)

    for index, row in enumerate(ordered):
        window = []
        for previous in ordered[max(0, index - 6): index + 1]:
            if previous["attendance_rate"] is not None:
                window.append(previous["attendance_rate"])
        row["moving_avg_7d"] = _round_half_up(sum(window) / len(window), 4) if window else None

    return {"department": department, "items": ordered}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    valid = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in valid:
        raise HTTPException(status_code=422, detail="unknown endpoint")

    if endpoint == "attendance_list":
        filter_doc: dict[str, Any] = {}
        if emp_code:
            filter_doc["emp_code"] = emp_code
        if date_from or date_to:
            filter_doc["date"] = {}
            if date_from:
                filter_doc["date"]["$gte"] = date_from
            if date_to:
                filter_doc["date"]["$lte"] = date_to
        if status:
            filter_doc["status"] = status
        explain = db.attendance_logs.find(filter_doc).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, detail="emp_code and month are required")
        begin, end = _date_range(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": begin, "$lt": end}}}, {"$sort": {"date": -1}}]
        explain = db.attendance_logs.aggregate(pipeline).explain() if hasattr(db.attendance_logs.aggregate(pipeline), "explain") else db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, detail="month is required")
        begin, end = _date_range(month)
        pipeline = [{"$match": {"date": {"$gte": begin, "$lt": end}}}, {"$group": {"_id": "$emp_code", "records": {"$push": "$date"}}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, detail="month is required")
        begin, end = _date_range(month)
        pipeline = [{"$match": {"date": {"$gte": begin, "$lt": end}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}}, {"$sort": {"total_late_minutes": -1, "_id": 1}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, detail="department, from and to are required")
        pipeline = [{"$match": {"date": {"$gte": from_date, "$lte": to_date}}}, {"$group": {"_id": "$date", "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, 1, 0]}}}}]
        explain = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    raise HTTPException(status_code=422, detail="unsupported endpoint")

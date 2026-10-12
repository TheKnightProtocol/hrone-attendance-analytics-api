"""Employee attendance and analytics API."""
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid date") from exc


def epoch(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 100_000_000_000 <= value <= 4_102_444_800_000:
        raise ValueError("must be epoch milliseconds")
    return value


def to_dt(value: Optional[int]) -> datetime:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0, tzinfo=None)


def to_epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return int((value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp() * 1000)


def mins(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def local(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC).astimezone(IST) if value.tzinfo is None else value.astimezone(IST)


def attendance_date(value: datetime, employee: dict[str, Any]) -> str:
    moment = local(value)
    day = moment.date()
    if mins(employee["shift_end"]) <= mins(employee["shift_start"]) and mins(employee["shift_end"]) > moment.hour * 60 + moment.minute:
        day -= timedelta(days=1)
    return day.isoformat()


def boundary(record_date: str, employee: dict[str, Any], field: str) -> datetime:
    day = parse_date(record_date)
    if field == "shift_end" and mins(employee["shift_end"]) <= mins(employee["shift_start"]):
        day += timedelta(days=1)
    hours, minutes = employee[field].split(":")
    return datetime.combine(day, time(int(hours), int(minutes)), IST).astimezone(UTC).replace(tzinfo=None)


def rounded(value: Decimal, places: str = "0.01") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def derived(punch_in: Optional[datetime], punch_out: Optional[datetime], employee: dict[str, Any], record_date: str) -> dict[str, Any]:
    if punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    difference = punch_in - boundary(record_date, employee, "shift_start")
    result = {"work_hours": None, "late_minutes": int(difference.total_seconds() // 60) if difference.total_seconds() > 600 else 0, "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        hours = rounded(Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600"))
        overtime = int((punch_out - boundary(record_date, employee, "shift_end")).total_seconds() // 60)
        result.update(work_hours=hours, overtime_minutes=overtime if overtime >= 30 else 0, half_day=hours < 4.5)
    return result


def record_response(doc: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in doc.items() if key != "_id"}
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result["punch_in"] = to_epoch(result.get("punch_in"))
    result["punch_out"] = to_epoch(result.get("punch_out"))
    for entry in result["history"]:
        entry["at"] = to_epoch(entry["at"])
        for change in entry["changes"].values():
            for key in ("from", "to"):
                if isinstance(change.get(key), datetime):
                    change[key] = to_epoch(change[key])
    return result


def employee_response(doc: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in doc.items() if key != "_id"}
    result["created_at"] = to_epoch(result["created_at"])
    return result


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def distinct_shift(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return None if value is None else epoch(value)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in PRESENCE:
            raise ValueError("invalid presence status")
        return value


class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_punched_at(cls, value: Optional[int]) -> Optional[int]:
        return None if value is None else epoch(value)


class RegularizeIn(StrictModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in STATUSES:
            raise ValueError("invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_punch(cls, value: Optional[int]) -> Optional[int]:
        return None if value is None else epoch(value)


@app.on_event("startup")
def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise HTTPException(503, "database unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return employee_response(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_response(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    moment = to_dt(body.punched_at)
    record_date = attendance_date(moment, employee)
    doc = {"emp_code": body.emp_code, "date": record_date, "status": body.status, "punch_in": moment, "punch_out": None,
           **derived(moment, None, employee, record_date), "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return record_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    moment = to_dt(body.punched_at)
    candidate = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": moment}}, sort=[("punch_in", DESCENDING)])
    if not candidate:
        raise HTTPException(404, "no punch-in found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    elapsed = (moment - candidate["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 86400:
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    values = derived(candidate["punch_in"], moment, employee, candidate["date"])
    updated = db.attendance_logs.find_one_and_update({"_id": candidate["_id"], "punch_out": None}, {"$set": {"punch_out": moment, **values}}, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_response(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    if date_from: parse_date(date_from)
    if date_to: parse_date(date_to)
    if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must not be after date_to")
    if status and status not in STATUSES: raise HTTPException(422, "invalid status")
    query: dict[str, Any] = {key: value for key, value in (("emp_code", emp_code), ("status", status)) if value}
    if date_from or date_to: query["date"] = {key: value for key, value in (("$gte", date_from), ("$lte", date_to)) if value}
    total = db.attendance_logs.count_documents(query)
    docs = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_response(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn) -> dict[str, Any]:
    record_date = date
    parse_date(record_date)
    employee = db.employees.find_one({"emp_code": emp_code})
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": record_date})
    if not employee or not current: raise HTTPException(404, "employee or attendance record not found")
    requested = body.model_dump(exclude_none=True)
    status = requested.get("status", current["status"])
    if status in {"ABSENT", "LEAVE"} and ("punch_in" in requested or "punch_out" in requested): raise HTTPException(422, "absence cannot have punch times")
    new_in = to_dt(requested["punch_in"]) if "punch_in" in requested else current.get("punch_in")
    new_out = to_dt(requested["punch_out"]) if "punch_out" in requested else current.get("punch_out")
    if status in {"ABSENT", "LEAVE"}: new_in = new_out = None
    if status in PRESENCE and new_in is None: raise HTTPException(422, "presence status requires punch_in")
    if new_in is not None and attendance_date(new_in, employee) != record_date: raise HTTPException(422, "punch_in does not belong to record date")
    if new_out is not None and (new_in is None or new_out <= new_in or (new_out - new_in).total_seconds() > 86400): raise HTTPException(422, "invalid punch_out")
    values = {"status": status, "punch_in": new_in, "punch_out": new_out, **(derived(new_in, new_out, employee, record_date) if status in PRESENCE else {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False})}
    changes = {key: {"from": current.get(key), "to": value} for key, value in values.items() if current.get(key) != value}
    if not changes: raise HTTPException(422, "request changes nothing")
    entry = {"at": datetime.now(UTC).replace(tzinfo=None, microsecond=0), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update({"_id": current["_id"], "history": {"$size": len(current.get("history", []))}}, {"$set": values, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409, "attendance record changed concurrently")
    return record_response(updated)


def month_bounds(month: str) -> tuple[str, str]:
    if len(month) != 7 or month[4] != "-": raise HTTPException(422, "invalid month")
    try: first = date.fromisoformat(month + "-01")
    except ValueError as exc: raise HTTPException(422, "invalid month") from exc
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first.isoformat(), last.isoformat()


def workdays(first: date, last: date) -> int:
    return sum((first + timedelta(days=index)).weekday() < 5 for index in range((last - first).days + 1))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str) -> dict[str, Any]:
    first, last = month_bounds(month); employee = db.employees.find_one({"emp_code": emp_code})
    if not employee: raise HTTPException(404, "employee not found")
    start = max(parse_date(first), parse_date(employee["joined_on"])); end = parse_date(last)
    working = workdays(start, end) if start <= end else 0
    present = Decimal("0"); leave = late_count = total_late = total_overtime = 0
    for doc in db.attendance_logs.aggregate([{"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}}]):
        if doc.get("status") in PRESENCE and parse_date(doc["date"]).weekday() < 5: present += Decimal("0.5") if doc.get("half_day", False) else Decimal("1")
        leave += doc.get("status") == "LEAVE"; late_count += doc.get("late_minutes", 0) > 0; total_late += doc.get("late_minutes", 0); total_overtime += doc.get("overtime_minutes", 0)
    return {"emp_code": emp_code, "month": month, "working_days": working, "present_days": float(present), "leave_days": leave, "late_count": late_count, "total_late_minutes": total_late, "total_overtime_minutes": total_overtime, "attendance_pct": None if not working else rounded(present / Decimal(working) * 100)}


def employees_for(month: str, department: Optional[str] = None) -> tuple[str, str, list[dict[str, Any]]]:
    first, last = month_bounds(month); query: dict[str, Any] = {"joined_on": {"$lte": last}}
    if department: query["department"] = department
    return first, last, list(db.employees.find(query, {"_id": 0}))


def explain_aggregate(pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None) -> dict[str, Any]:
    first, last, employees = employees_for(month, department); groups: dict[str, dict[str, Any]] = {}
    by_code = {employee["emp_code"]: employee for employee in employees}
    for employee in employees: groups.setdefault(employee["department"], {"department": employee["department"], "headcount": 0, "present_days": Decimal("0"), "hours": [], "late_count": 0, "total_late_minutes": 0, "leave_count": 0, "on_duty_count": 0})["headcount"] += 1
    for doc in db.attendance_logs.aggregate([{"$match": {"emp_code": {"$in": list(by_code)}, "date": {"$gte": first, "$lte": last}}}]):
        item = groups[by_code[doc["emp_code"]]["department"]]
        if doc.get("status") in PRESENCE and parse_date(doc["date"]).weekday() < 5: item["present_days"] += Decimal("0.5") if doc.get("half_day", False) else Decimal("1")
        if doc.get("status") in PRESENCE and doc.get("work_hours") is not None: item["hours"].append(Decimal(str(doc["work_hours"])))
        item["late_count"] += doc.get("late_minutes", 0) > 0; item["total_late_minutes"] += doc.get("late_minutes", 0); item["leave_count"] += doc.get("status") == "LEAVE"; item["on_duty_count"] += doc.get("status") == "ON_DUTY"
    result = []
    for item in sorted(groups.values(), key=lambda value: value["department"]):
        hours = item.pop("hours"); item["present_days"] = float(item["present_days"]); item["avg_work_hours"] = rounded(sum(hours, Decimal("0")) / len(hours)) if hours else None; result.append(item)
    return {"month": month, "items": result}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None) -> dict[str, Any]:
    first, last, employees = employees_for(month, department); by_code = {employee["emp_code"]: employee for employee in employees}; totals: dict[str, list[int]] = {}
    for doc in db.attendance_logs.aggregate([{"$match": {"emp_code": {"$in": list(by_code)}, "date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}}, {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "count": {"$sum": 1}}}]): totals[doc["_id"]] = [doc["total"], doc["count"]]
    ordered = sorted(totals, key=lambda code: (-totals[code][0], code)); result = []
    for code in ordered:
        rank = 1 + sum(totals[other][0] > totals[code][0] for other in ordered)
        if rank <= limit: result.append({"rank": rank, "emp_code": code, "name": by_code[code]["name"], "department": by_code[code]["department"], "total_late_minutes": totals[code][0], "late_count": totals[code][1]})
    return {"month": month, "items": result}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)) -> dict[str, Any]:
    start, end = parse_date(from_), parse_date(to)
    if end < start or (end - start).days > 91: raise HTTPException(422, "invalid trend range")
    employees = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not employees: raise HTTPException(404, "department not found")
    codes = [employee["emp_code"] for employee in employees]; logs = list(db.attendance_logs.aggregate([{"$match": {"emp_code": {"$in": codes}, "date": {"$gte": from_, "$lte": to}}}]))
    result = []
    for index in range((end - start).days + 1):
        current = start + timedelta(days=index); day_logs = [log for log in logs if log["date"] == current.isoformat()]; headcount = sum(parse_date(employee["joined_on"]) <= current for employee in employees); present = sum(Decimal("0.5") if log.get("half_day", False) else Decimal("1") for log in day_logs if log.get("status") in PRESENCE); rate = rounded(present / Decimal(headcount), "0.0001") if current.weekday() < 5 and headcount else None
        row = {"date": current.isoformat(), "is_working_day": current.weekday() < 5, "headcount": headcount, "present_count": float(present), "late_count": sum(log.get("late_minutes", 0) > 0 for log in day_logs), "attendance_rate": rate, "moving_avg_7d": None}; result.append(row); rates = [item["attendance_rate"] for item in result[-7:] if item["attendance_rate"] is not None]; row["moving_avg_7d"] = rounded(sum((Decimal(str(value)) for value in rates), Decimal("0")) / len(rates), "0.0001") if rates else None
    return {"department": department, "items": result}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None, limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None, from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    if endpoint == "attendance_list":
        query: dict[str, Any] = {key: value for key, value in (("emp_code", emp_code), ("status", status)) if value};
        if date_from or date_to: query["date"] = {key: value for key, value in (("$gte", date_from), ("$lte", date_to)) if value}
        explanation = db.attendance_logs.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size).explain("executionStats")
    else:
        if endpoint not in {"employee_monthly", "department_summary", "late_leaderboard", "department_trend"}: raise HTTPException(422, "unknown endpoint")
        if endpoint == "employee_monthly" and (not emp_code or not month): raise HTTPException(422, "emp_code and month are required")
        if endpoint in {"department_summary", "late_leaderboard"} and not month: raise HTTPException(422, "month is required")
        if endpoint == "department_trend" and (not department or not from_ or not to): raise HTTPException(422, "department, from and to are required")
        explanation = explain_aggregate([{"$match": {"emp_code": emp_code}}] if endpoint == "employee_monthly" else [{"$match": {"date": {"$exists": True}}}])
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explanation}
"""Employee Attendance & Analytics API.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is read, real env vars win)
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()
log = logging.getLogger("attendance")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db.employees
logs = db.attendance_logs

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = 4.5
MAX_SHIFT = timedelta(hours=24)
MAX_TREND_DAYS = 92

EpochMs = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Page = Annotated[int, Query(ge=1, le=1_000_000)]
PageSize = Annotated[int, Query(ge=1, le=100)]

_indexes_ready = False


# --------------------------------------------------------------------------- #
# Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes():
    global _indexes_ready
    employees.create_indexes([
        IndexModel([("emp_code", ASCENDING)], unique=True),
        IndexModel([("department", ASCENDING), ("emp_code", ASCENDING)]),
        IndexModel([("joined_on", ASCENDING)]),
    ])
    logs.create_indexes([
        # one record per employee per day, and the lookup key for per-employee queries
        IndexModel([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True),
        # default listing order, month ranges for the cross-employee analytics
        IndexModel([("date", DESCENDING), ("emp_code", ASCENDING)]),
        IndexModel([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)]),
        # "latest punch-in at or before X" for punch-out
        IndexModel([("emp_code", ASCENDING), ("punch_in", DESCENDING)]),
    ])
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app):
    try:
        ensure_indexes()
    except PyMongoError:
        log.exception("could not create indexes at startup, /health will retry")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
def mongo_down(_request, exc):
    return JSONResponse({"detail": "database unavailable"}, status_code=503)


# --------------------------------------------------------------------------- #
# Small helpers: errors, time, rounding, serialisation
# --------------------------------------------------------------------------- #
def bad(msg: str):
    raise HTTPException(422, [{"type": "value_error", "msg": msg}])


def to_dt(ms: Optional[int]) -> datetime:
    """Epoch millis -> UTC datetime truncated to whole seconds (R1)."""
    if ms is None:
        return datetime.now(UTC).replace(microsecond=0)
    return (EPOCH + timedelta(milliseconds=ms)).replace(microsecond=0)


def to_ms(value):
    """datetime -> epoch millis; anything else is returned untouched."""
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (value - EPOCH) // timedelta(milliseconds=1)


def half_up(value, places: int):
    if value is None:
        return None
    exact = value if isinstance(value, Decimal) else Decimal(repr(round(value, 9)))
    return float(exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        bad(f"invalid date: {text}")


def month_span(month: str) -> tuple[date, date]:
    year, mon = map(int, month.split("-"))
    try:
        return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])
    except ValueError:
        bad(f"invalid month: {month}")


def hm(text: str):
    return datetime.strptime(text, "%H:%M").time()


def weekdays_between(first: date, last: date) -> int:
    days = (last - first).days + 1
    if days <= 0:
        return 0
    weeks, extra = divmod(days, 7)
    return weeks * 5 + sum(1 for i in range(extra) if (first.weekday() + i) % 7 < 5)


def employee_out(doc):
    keys = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")
    return {**{k: doc.get(k) for k in keys}, "created_at": to_ms(doc.get("created_at"))}


def record_out(doc):
    history = [
        {
            "at": to_ms(h["at"]),
            "by": h["by"],
            "reason": h["reason"],
            "changes": {k: {"from": to_ms(v["from"]), "to": to_ms(v["to"])} for k, v in h["changes"].items()},
        }
        for h in doc.get("history") or []
    ]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": history,
    }


def page_out(items, total, page, page_size):
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def get_employee(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def attendance_day(emp: dict, punch_in: datetime) -> date:
    """R1: IST date of the punch-in; early punches on an overnight shift belong to yesterday."""
    local = punch_in.astimezone(IST)
    overnight = hm(emp["shift_end"]) <= hm(emp["shift_start"])
    if overnight and local.time() < hm(emp["shift_end"]):
        return local.date() - timedelta(days=1)
    return local.date()


def shift_times(emp: dict, day: date) -> tuple[datetime, datetime]:
    start_t, end_t = hm(emp["shift_start"]), hm(emp["shift_end"])
    start = datetime.combine(day, start_t, IST)
    end = datetime.combine(day, end_t, IST)
    if end_t <= start_t:
        end += timedelta(days=1)
    return start, end


def derive(emp: dict, day: date, punch_in: datetime, punch_out: Optional[datetime]) -> dict:
    """R2 - R5. Everything the API computes and stores for a presence record."""
    start, end = shift_times(emp, day)
    late_secs = int((punch_in - start).total_seconds())
    out = {
        "work_hours": None,
        "late_minutes": late_secs // 60 if late_secs > GRACE_SECONDS else 0,
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is None:
        return out
    worked = Decimal(int((punch_out - punch_in).total_seconds())) / 3600
    out["work_hours"] = half_up(worked, 2)
    out["half_day"] = out["work_hours"] < HALF_DAY_HOURS
    overtime = int((punch_out - end).total_seconds()) // 60
    out["overtime_minutes"] = overtime if overtime >= MIN_OVERTIME_MINUTES else 0
    return out


NO_WORK = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def check_span(punch_in: datetime, punch_out: datetime):
    if punch_out <= punch_in:
        bad("punch_out must be after punch_in")
    if punch_out - punch_in > MAX_SHIFT:
        bad("punch_out is more than 24 hours after punch_in")


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM_RE)
    shift_end: str = Field("18:30", pattern=HHMM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @model_validator(mode="after")
    def check_values(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        date.fromisoformat(self.joined_on)
        return self


class PunchIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOut(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class Regularize(BaseModel):
    # Defaults are not validated by pydantic, so an explicit null is still a 422.
    status: Status = None
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System, employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    client.admin.command("ping")
    if not _indexes_ready:
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department else {}
    docs = employees.find(query).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    total = employees.count_documents(query)
    return page_out([employee_out(d) for d in docs], total, page, page_size)


# --------------------------------------------------------------------------- #
# Query plans: one builder per endpoint, shared by the endpoint and /admin/explain
# --------------------------------------------------------------------------- #
def run(plan: dict):
    coll = db[plan["coll"]]
    if "pipeline" in plan:
        return list(coll.aggregate(plan["pipeline"]))
    f = plan["find"]
    return list(coll.find(f["filter"], f["projection"]).sort(list(f["sort"].items())).skip(f["skip"]).limit(f["limit"]))


def explain(plan: dict) -> dict:
    if "pipeline" in plan:
        inner = {"aggregate": plan["coll"], "pipeline": plan["pipeline"], "cursor": {}}
    else:
        inner = {"find": plan["coll"], **plan["find"]}
    result = db.command({"explain": inner, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))


def attendance_plan(emp_code, date_from, date_to, status, page, page_size) -> dict:
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        lo = parse_date(date_from) if date_from else None
        hi = parse_date(date_to) if date_to else None
        if lo and hi and lo > hi:
            bad("date_from is after date_to")
        query["date"] = {**({"$gte": date_from} if lo else {}), **({"$lte": date_to} if hi else {})}
    if status:
        query["status"] = status
    return {
        "coll": "attendance_logs",
        "find": {
            "filter": query,
            "projection": {"_id": 0},
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        },
    }


# --- aggregation building blocks (all read the STORED derived values) ------- #
def _n(field):
    return {"$ifNull": [field, 0]}


IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
IS_WEEKDAY = {"$in": [
    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "onError": None, "onNull": None}}},
    [2, 3, 4, 5, 6],  # Mon..Fri, $dayOfWeek counts Sunday as 1
]}
DAY_WEIGHT = {"$cond": [IS_PRESENT, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$gt": ["$work_hours", None]}]}


def total_if(cond, then=1):
    return {"$sum": {"$cond": [cond, then, 0]}}


MONTH_TOTALS = {
    "present_days": total_if(IS_WEEKDAY, DAY_WEIGHT),
    "leave_days": total_if({"$eq": ["$status", "LEAVE"]}),
    "on_duty_days": total_if({"$eq": ["$status", "ON_DUTY"]}),
    "late_count": total_if({"$gt": [_n("$late_minutes"), 0]}),
    "late_minutes": {"$sum": _n("$late_minutes")},
    "overtime_minutes": {"$sum": _n("$overtime_minutes")},
    "hours_sum": total_if(HAS_HOURS, "$work_hours"),
    "hours_n": total_if(HAS_HOURS),
}


def monthly_plan(emp_code: str, month: str) -> dict:
    first, last = month_span(month)
    return {
        "coll": "attendance_logs",
        "pipeline": [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
            {"$group": {"_id": None, **MONTH_TOTALS}},
        ],
    }


def summary_plan(month: str, department: Optional[str]) -> dict:
    first, last = month_span(month)
    match = {"joined_on": {"$lte": last.isoformat()}}  # R9
    if department:
        match["department"] = department
    return {
        "coll": "employees",
        "pipeline": [
            {"$match": match},
            # employee-driven, so people with no logs still count towards headcount
            {"$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {"$group": {"_id": None, **MONTH_TOTALS}},
                ],
                "as": "m",
            }},
            {"$set": {"m": {"$first": "$m"}}},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{k: {"$sum": f"$m.{k}"} for k in MONTH_TOTALS},
            }},
            {"$sort": {"_id": 1}},
        ],
    }


def leaderboard_plan(month: str, department: Optional[str], limit: int) -> dict:
    first, last = month_span(month)
    pipeline = [
        {"$match": {
            "date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
            "late_minutes": {"$gt": 0},
        }},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees drop out here
    ]
    if department:
        pipeline.append({"$match": {"e.department": department}})
    pipeline += [
        {"$project": {
            "_id": 0, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
        # $rank is standard competition ranking: ties share a rank, the next one is skipped
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ]
    return {"coll": "attendance_logs", "pipeline": pipeline}


def trend_plan(department: str, first: date, last: date) -> dict:
    days = (last - first).days + 1
    day_start = datetime(first.year, first.month, first.day, tzinfo=UTC)
    return {
        "coll": "employees",
        "pipeline": [
            {"$match": {"department": department}},
            {"$group": {"_id": None, "joined": {"$push": "$joined_on"}, "codes": {"$push": "$emp_code"}}},
            # an array in localField matches any of its elements
            {"$lookup": {
                "from": "attendance_logs",
                "localField": "codes",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                    {"$group": {
                        "_id": "$date",
                        "present": {"$sum": DAY_WEIGHT},
                        "late": total_if({"$gt": [_n("$late_minutes"), 0]}),
                    }},
                ],
                "as": "daily",
            }},
            # one row per calendar day, built from a range so empty days still exist
            {"$set": {"offset": {"$range": [0, days]}}},
            {"$unwind": "$offset"},
            {"$set": {"d": {"$dateAdd": {"startDate": day_start, "unit": "day", "amount": "$offset"}}}},
            {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}}}},
            {"$set": {
                "stats": {"$first": {"$filter": {
                    "input": "$daily", "as": "x", "cond": {"$eq": ["$$x._id", "$date"]},
                }}},
                "headcount": {"$size": {"$filter": {
                    "input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$date"]},
                }}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},
            }},
            {"$set": {"present_count": _n("$stats.present"), "late_count": _n("$stats.late")}},
            {"$set": {"attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]}}},
            {"$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }},
            {"$sort": {"date": 1}},
            {"$project": {
                "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
            }},
        ],
    }


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchIn):
    emp = get_employee(body.emp_code)
    ts = to_dt(body.punched_at)
    day = attendance_day(emp, ts)
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(emp, day, ts, None),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # the unique (emp_code, date) index decides who wins a race
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOut):
    emp = get_employee(body.emp_code)
    ts = to_dt(body.punched_at)
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    check_span(rec["punch_in"], ts)
    day = date.fromisoformat(rec["date"])
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},  # only the first of two racing requests matches
        {"$set": {"punch_out": ts, **derive(emp, day, rec["punch_in"], ts)}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    plan = attendance_plan(emp_code, date_from, date_to, status, page, page_size)
    total = logs.count_documents(plan["find"]["filter"])
    return page_out([record_out(d) for d in run(plan)], total, page, page_size)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: Regularize, date: str = Path(pattern=DATE_RE)):
    # `date` shadows the datetime.date class in here, so only parse_date() is used
    record_day = parse_date(date)
    emp = get_employee(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    status = body.status or rec["status"]
    if status in PRESENT_STATUSES:
        new_in = to_dt(body.punch_in) if body.punch_in else rec.get("punch_in")
        new_out = to_dt(body.punch_out) if body.punch_out else rec.get("punch_out")
        if new_in is None:
            bad("a presence status needs a punch_in")
        if body.punch_in and attendance_day(emp, new_in) != record_day:
            bad("punch_in must stay on the record's attendance date")
        if new_out is not None and (body.punch_in or body.punch_out):
            check_span(new_in, new_out)
        derived = derive(emp, record_day, new_in, new_out)
    else:
        if body.punch_in or body.punch_out:
            bad("ABSENT and LEAVE clear the punch times, do not send them")
        new_in = new_out = None
        derived = NO_WORK

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    new = {"status": status, "punch_in": new_in, "punch_out": new_out, **derived}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        bad("nothing to change")

    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic lock: the write only lands if the record is still what we read.
    unchanged = {
        "_id": rec["_id"],
        "punch_in": old["punch_in"],
        "punch_out": old["punch_out"],
        "history": {"$size": len(rec["history"])} if "history" in rec else {"$exists": False},
    }
    updated = logs.find_one_and_update(
        unchanged, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(409, "record was changed by someone else, retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=MONTH_RE)):
    emp = get_employee(emp_code)
    first, last = month_span(month)
    joined = parse_date(emp["joined_on"])
    working_days = weekdays_between(max(first, joined), last)  # R7
    totals = next(iter(run(monthly_plan(emp_code, month))), {})
    present = totals.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(present, 2),
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("late_minutes", 0),
        "total_overtime_minutes": totals.get("overtime_minutes", 0),
        "attendance_pct": half_up(present / working_days * 100, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(pattern=MONTH_RE), department: Optional[str] = None):
    items = []
    for row in run(summary_plan(month, department)):
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": half_up(row["present_days"], 2),
            "avg_work_hours": half_up(row["hours_sum"] / row["hours_n"], 2) if row["hours_n"] else None,
            "late_count": row["late_count"],
            "total_late_minutes": row["late_minutes"],
            "leave_count": row["leave_days"],
            "on_duty_count": row["on_duty_days"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    return {"month": month, "items": run(leaderboard_plan(month, department, limit))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: str = Query(alias="from", pattern=DATE_RE),
    end: str = Query(alias="to", pattern=DATE_RE),
):
    first, last = parse_date(start), parse_date(end)
    if last < first:
        bad("`to` is before `from`")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        bad(f"range is longer than {MAX_TREND_DAYS} days")
    if not employees.count_documents({"department": department}, limit=1):
        raise HTTPException(404, "unknown department")
    rows = run(trend_plan(department, first, last))
    for row in rows:
        row["attendance_rate"] = half_up(row["attendance_rate"], 4)
        row["moving_avg_7d"] = half_up(row["moving_avg_7d"], 4)
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    start: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    end: Optional[str] = Query(None, alias="to", pattern=DATE_RE),
    page: Page = 1,
    page_size: PageSize = 20,
):
    required = {
        "employee_monthly": {"emp_code": emp_code, "month": month},
        "department_summary": {"month": month},
        "late_leaderboard": {"month": month},
        "department_trend": {"department": department, "from": start, "to": end},
    }.get(endpoint, {})
    missing = [name for name, value in required.items() if value is None]
    if missing:
        bad(f"missing query parameter(s): {', '.join(missing)}")

    plan = {
        "attendance_list": lambda: attendance_plan(emp_code, date_from, date_to, status, page, page_size),
        "employee_monthly": lambda: monthly_plan(emp_code, month),
        "department_summary": lambda: summary_plan(month, department),
        "late_leaderboard": lambda: leaderboard_plan(month, department, limit),
        "department_trend": lambda: trend_plan(department, parse_date(start), parse_date(end)),
    }[endpoint]()
    return {"endpoint": endpoint, "collection": plan["coll"], "explain": explain(plan)}

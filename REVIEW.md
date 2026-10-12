# REVIEW.md

Defects found in the starter `app/main.py`, and what I did about each.

| # | Where | What is wrong | How you'd notice it | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Floors to whole minutes first, then tests `> 10`. 09:40:01 floors to 10, so it is treated as on time. | Shift 09:30, punch 09:40:01 -> expect 10, get 0 | Compare raw seconds against the 600s grace, floor only after that |
| 2 | `compute_late_minutes` | `punch_in.replace(hour=...)` uses the datetime's own zone (server local/naive), not IST. No overnight handling, so the shift start lands on the wrong day. Seconds are not truncated. | Run the server in UTC; every late value shifts by 5.5h. 22:00 shift, punch at 02:00 | `attendance_day` + `shift_times` build IST datetimes and handle overnight shifts |
| 3 | `compute_work_hours` | Built-in `round()` is float/banker's rounding, not half-up. | 3 h 21 min 36 s style inputs landing on exactly .xx5 round the wrong way | `Decimal` on integer seconds, `ROUND_HALF_UP` |
| 4 | `compute_overtime` | No 30-minute minimum. Mixes an IST-aware `end` with a naive `punch_out`, which raises `TypeError`. Overnight shift end is put on the same day, so overtime is hugely negative/zero. | Any punch-out call, 22:00-06:00 shift | Rewritten inside `derive`, uses real shift end and the 30 minute rule |
| 5 | `half_day` | Never computed anywhere. Always false. | Punch out after 3 hours, `half_day` stays false | `derive` sets it from the rounded `work_hours < 4.5` |
| 6 | `EmployeeIn` | No validation: any `emp_code`, bad email, `"25:99"` shift times, equal start and end, `joined_on` can be garbage. | POST `{"emp_code":"x","joined_on":"nope"}` returns 201 | Field patterns and a model validator, so bad input gives 422 |
| 7 | `create_employee` | Check-then-insert, so two parallel creates can both pass. `created_at` is naive local time and returned as a datetime, not epoch ms. | Fire two identical POSTs at once | Unique index on `emp_code`, `DuplicateKeyError` -> 409; UTC `created_at` returned as ms |
| 8 | `list_employees` | `skip = page * page_size` skips the whole first page. `total` ignores the department filter. No sort, so pages are unstable. `page_size=0` or negative is not rejected. | `GET /employees?page=1` misses the first rows; filtered total is wrong | `(page-1)*size`, count with the same filter, sort by `emp_code`, bounded params |
| 9 | `punch_in` | Unknown employee: `emp` is `None`, `emp["shift_start"]` raises, 500. | Punch in with `EMP9999` | `get_employee` -> 404 |
| 10 | `punch_in` | Check-then-insert race, two requests both insert. | Two parallel punch-ins | Unique `(emp_code, date)` index, duplicate key -> 409 |
| 11 | `punch_in` | `datetime.fromtimestamp(ms/1000)` is server local time; `if body.punched_at` treats `0` as missing; the date is local, not IST (R1); no overnight rule; seconds not truncated. | Server in UTC, punch at 01:00 IST gets the previous date | `to_dt` (UTC, whole seconds) and `attendance_day` (IST, overnight aware) |
| 12 | `PunchInIn.status` | Any string accepted, including `ABSENT`, `LEAVE` or junk. `punched_at` accepts seconds, negatives, anything. | `status: "banana"` returns 201 | `Literal` for presence statuses, strict bounded `EpochMs` -> 422 |
| 13 | `punch_in` response | Returns `id` (the ObjectId), naive datetimes serialized as ISO strings. Contract wants no ids and epoch ms. | Compare to openapi `AttendanceRecord` | `record_out` serializer |
| 14 | `list_attendance` | `find(q)` pulls every match into memory, sorts and slices in Python. | 100k records: slow, memory heavy | Sort, skip and limit in MongoDB, backed by indexes |
| 15 | `list_attendance` | Sorted by `date` only, no `emp_code` tie-break; leaks `id`; legacy rows without `history`/`half_day` come back without them; `page=0` slices with negative index; `date_from > date_to` not rejected; dates unvalidated. | Page through a day with many records; `page=0` | Full sort key, `record_out` defaults, bounded params, 422 on bad ranges |
| 16 | `/health` | Always returns ok, even if MongoDB is down. | Stop mongod, `/health` is still 200 | Pings MongoDB, failure is a 503 |
| 17 | Client setup | `MongoClient` without `tz_aware`, so every datetime read back is naive and silently assumed local. | Compare a stored punch to a computed one: `TypeError` | `tz_aware=True`, everything is UTC internally, IST only for rules |
| 18 | whole app | No indexes at all. | Explain shows COLLSCAN | Indexes created at startup, idempotent |

## Looked at, decided it was fine

- `doc.pop("_id")` after `insert_one`: it is needed because pymongo adds `_id` to the dict. I kept the idea but now serialize through explicit `*_out` functions.
- `load_dotenv()` with defaults: it does not override real environment variables, which is what the contract asks for.
- A fixed `IST = UTC+5:30`: India has no daylight saving, so a fixed offset is correct.
- `status_code=201` on create endpoints: matches the contract.

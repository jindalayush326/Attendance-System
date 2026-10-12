# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`, `(department, emp_code)` for the filtered, sorted list, and `joined_on` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, per-employee queries), `(date desc, emp_code)` for the default listing and month ranges, `(status, date desc, emp_code)` for status filters, and `(emp_code, punch_in desc)` to find the open record at punch-out. I rejected an index on `late_minutes`: the date range already narrows the scan a lot, and it would only slow writes.

2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets one insert succeed (201); the other gets `DuplicateKeyError`, which I turn into a 409. There is no read before the write.

3. **Ties.** I use `$rank`, so tied employees share a rank and the next one is skipped. `limit` is applied to the rank, so a tie at the cutoff returns extra rows rather than dropping one at random. Rows are then sorted by minutes, then `emp_code`.

4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks up each person's logs, so someone with zero logs still produces a row and is counted.

5. **At 100x.** I would pre-aggregate a daily per-department rollup instead of reading raw logs for the trend and summary.

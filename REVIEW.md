# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always returns 200 without checking MongoDB. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 on failure. |
| 2 | `create_employee` | Check-then-insert allows duplicate employee codes under concurrency; dates are not API epoch values. | Send concurrent creates with the same code. | Unique index plus duplicate-key handling; serialize `created_at`. |
| 3 | `list_employees` | Page one skips the first page, and `total` ignores the department filter. | Request page 1 or filter by department. | Use `(page - 1) * page_size`, filtered count, and stable sorting. |
| 4 | `punch_in` | Unknown employees cause a server error; local naive time and ordinary calendar dates break IST and overnight rules. | Punch an unknown employee or use an overnight shift. | Validate employee, normalize UTC milliseconds, calculate the R1 date, and use an atomic unique key. |
| 5 | `compute_late_minutes` | Grace-boundary and whole-minute behavior are incorrect for sub-minute timestamps. | Test 09:40:00.001 and 10:15:59. | Truncate instants and apply the strict 10-minute rule. |
| 6 | `compute_work_hours` | Python `round` uses banker rounding rather than required half-up rounding. | Use a duration ending in an exact half-centisecond value. | Use `Decimal` with `ROUND_HALF_UP`. |
| 7 | `compute_overtime` | Does not handle overnight shift ends and applies no 30-minute threshold. | Punch out after an overnight shift or 29 minutes late. | Build the shift boundary from the attendance date and apply R3. |
| 8 | `list_attendance` | Loads the whole collection into memory, has unstable ordering, and mutates Mongo documents by removing `_id`. | Run it against a large collection or request a second page. | Use indexed Mongo pagination, the required sort, and response serialization. |
| 9 | Missing endpoints | Punch-out, regularization, all analytics, and explain were absent. | The corresponding OpenAPI routes return 404. | Implemented all contract routes, including audit history and aggregations. |

The starter's use of `emp_code` rather than MongoDB `_id` was correct and was retained. The stored derived fields are also
treated as authoritative by analytics, as required; analytics do not recalculate them from punch timestamps.

Also note anything you looked at and decided was **not** a defect, and why.

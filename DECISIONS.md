# DECISIONS.md

1. **Indexes.** I create a unique `(emp_code, date)` index for the attendance invariant, a `(date, emp_code)` index for the
attendance list sort and range, a unique `emp_code` index for employee identity, and `(emp_code, punch_in)` for punch-out lookup.
I considered an index on `status`, but rejected it because status is low-cardinality and is not the leading predicate of the
important queries.

2. **Punch-in race.** Both requests calculate the same natural key. MongoDB accepts one insert through the unique index. The
other insert raises `DuplicateKeyError`, which the API converts to 409. There is no check-then-insert window.

3. **Ties.** Ranking counts how many totals are strictly greater, so tied employees receive the same standard competition rank.
The limit is applied to that rank, meaning every employee tied at the cutoff is returned.

4. **Headcount.** The summary starts from employees joined by the requested month, groups those employees by department, and
only then adds attendance logs. A department employee with no matching log therefore still contributes to headcount.

5. **100x data.** I would move the trend gap-filling and more analytics work fully into MongoDB aggregation, add targeted compound
indexes after measuring real explain plans, and cache immutable historical summaries rather than materializing logs in Python.

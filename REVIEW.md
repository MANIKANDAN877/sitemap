# Review

This code follows the assignment contract with a single FastAPI service in `app/main.py`. The implementation treats `emp_code` as the natural business key and keeps MongoDB `_id` values internal, matching the API contract.

Key review points:

- Validation is enforced at the API boundary for epoch milliseconds, status values, date ranges, and pagination limits.
- Derived fields are computed once at the point of punch-in/punch-out or adjustment and then stored, which keeps analytics consistent with the requirement to read stored values.
- History entries capture only actual field changes and are appended rather than overwritten, preserving the audit trail.

This is a focused implementation for the assignment and is designed to remain easy to reason about in a single-file structure.

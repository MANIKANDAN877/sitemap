# Design Decisions

## 1) Single-file FastAPI implementation

The assignment explicitly asks for the whole implementation in `app/main.py`, so the logic is kept together to make it easier to review and to reduce the chance of mismatched business rules across modules.

## 2) MongoDB indexes at startup

The API creates the minimal required indexes for `employees.emp_code`, `attendance_logs.emp_code + date`, and supporting lookups. This supports both the contract and the expected `explain` plans without resorting to a collection scan.

## 3) Derived values are authoritative

The app computes `late_minutes`, `work_hours`, `overtime_minutes`, and `half_day` once and stores them. Analytics endpoints read those values rather than recalculating from raw timestamps, which matches the assignment rules and prevents drift.

## 4) History as an audit trail

Manual corrections append a single `history` entry with `at`, `by`, `reason`, and a per-field `changes` object. This preserves the editing trail while keeping the record compact and easy to inspect.

## 5) Local testing compatibility

A mock-Mongo fallback is enabled via `USE_MOCK_MONGO=1`, which helps run the app in a minimal local environment without a live MongoDB server while still preserving the real MongoDB contract when the server is available.

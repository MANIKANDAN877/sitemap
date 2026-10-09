# Employee Attendance & Analytics API

This repository contains a FastAPI application for the HROne attendance assignment. It provides employee management, punch-in/punch-out flows, manual corrections with audit history, and MongoDB-backed analytics endpoints.

## Run locally

1. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Start a MongoDB instance locally (or set `MONGO_URI` / `MONGO_DB` to a remote instance).
3. Run the app:
   ```bash
   uvicorn app.main:app --port 8000 --reload
   ```

## Notes

- The app is implemented as a single file: `app/main.py`.
- It creates the required MongoDB indexes at startup.
- `USE_MOCK_MONGO=1` can be set in local tests when a real Mongo server is unavailable.
- The project includes a short review and design rationale in `REVIEW.md` and `DECISIONS.md`.

# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017  or connect with atlas" MONGO_DB="jindal_db"
python sample_seed.py
uvicorn app.main:app --port 8000
```
Needs MongoDB 6.0 or newer. Indexes are created on startup.

## Notes
- Everything in the assignment is implemented, including `/admin/explain/{endpoint}`.
- All endpoints and aggregation pipelines have been verified against MongoDB (confirming `IXSCAN` coverage and zero `COLLSCAN`).
- Moving average in the trend uses the unrounded daily rates, and only the final value is rounded.


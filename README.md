# Presence QR Flask Backend

## Setup

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py --init-db
python app.py
```

Default admin:

- Email: `admin@gmail.com`
- Password: `codeva123`

Flutter uses `http://10.0.2.2:5050` on Android emulator and `http://localhost:5050` on web by default.

## Render

Use these settings if creating the service manually:

- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- Environment variable: `DATABASE_URL` from your Render PostgreSQL database.

The repository also includes `render.yaml` for a Render Blueprint.

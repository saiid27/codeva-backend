import argparse
import os
import random
from contextlib import contextmanager
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt

import psycopg
from flask import Flask, jsonify, request
from flask_cors import CORS
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash


def load_env_file():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/presence_qr",
)


app = Flask(__name__)
CORS(app)
DB_INITIALIZED = False


@contextmanager
def db_conn():
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def today_token():
    return datetime.now().strftime("%Y%m%d")


def today_date():
    return datetime.now().strftime("%Y-%m-%d")


def now_time():
    return datetime.now().strftime("%H:%M")


def distance_meters(lat1, lon1, lat2, lon2):
    radius = 6371000
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = (
        sin(d_lat / 2) * sin(d_lat / 2)
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) * sin(d_lon / 2)
    )
    return 2 * radius * asin(sqrt(a))


def password_matches(stored_hash, password):
    if not stored_hash:
        return False
    if stored_hash == password:
        return True
    try:
        return check_password_hash(stored_hash, password)
    except ValueError:
        return False


def user_payload(user):
    return {
        "id": user["id"],
        "fullName": user["full_name"],
        "email": user["email"],
        "phone": user.get("phone") or "",
        "jobTitle": user.get("job_title") or "",
        "role": user["role"],
        "status": user["status"],
    }


def init_db():
    schema_statements = [
        """
    CREATE TABLE IF NOT EXISTS users (
      id BIGSERIAL PRIMARY KEY,
      full_name TEXT NOT NULL,
      email TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL,
      phone TEXT DEFAULT '',
      job_title TEXT DEFAULT '',
      role TEXT NOT NULL DEFAULT 'worker',
      status TEXT NOT NULL DEFAULT 'pending',
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
        """
    CREATE TABLE IF NOT EXISTS attendance (
      id BIGSERIAL PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      attend_date DATE NOT NULL,
      status TEXT NOT NULL,
      time TEXT,
      latitude DOUBLE PRECISION,
      longitude DOUBLE PRECISION,
      distance_m DOUBLE PRECISION,
      verification_reason TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(user_id, attend_date)
    )
    """,
        """
    CREATE TABLE IF NOT EXISTS email_otps (
      id BIGSERIAL PRIMARY KEY,
      email TEXT NOT NULL,
      code TEXT NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL,
      used BOOLEAN NOT NULL DEFAULT false,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
        """
    CREATE TABLE IF NOT EXISTS locations (
      id BIGSERIAL PRIMARY KEY,
      email TEXT NOT NULL,
      url TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
        """
    CREATE TABLE IF NOT EXISTS app_settings (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    ]
    with db_conn() as conn:
        for statement in schema_statements:
            conn.execute(statement)
        admin = conn.execute(
            "SELECT id FROM users WHERE email = %s",
            ("admin@gmail.com",),
        ).fetchone()
        admin_password = generate_password_hash("codeva123")
        if admin is None:
            conn.execute(
                """
                INSERT INTO users
                  (full_name, email, password_hash, phone, job_title, role, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                ("Admin", "admin@gmail.com", admin_password, "", "Admin", "admin", "approved"),
            )
        else:
            conn.execute(
                """
                UPDATE users
                   SET password_hash = %s, role = 'admin', status = 'approved'
                 WHERE email = 'admin@gmail.com'
                """,
                (admin_password,),
            )

        count = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"]
        if count <= 1:
            demo_password = generate_password_hash("1234")
            conn.execute(
                """
                INSERT INTO users
                  (full_name, email, password_hash, phone, job_title, role, status)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s),
                  (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (email) DO NOTHING
                """,
                (
                    "User Demo",
                    "user1@local",
                    demo_password,
                    "",
                    "Groupe A",
                    "worker",
                    "approved",
                    "User Pending",
                    "user2@local",
                    demo_password,
                    "",
                    "Groupe B",
                    "worker",
                    "pending",
                ),
            )
        defaults = {
            "company_latitude": "18.0735",
            "company_longitude": "-15.9582",
            "company_radius_m": "150",
        }
        for key, value in defaults.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO NOTHING
                """,
                (key, value),
            )


def get_company_location(conn):
    rows = conn.execute(
        """
        SELECT key, value
          FROM app_settings
         WHERE key IN ('company_latitude', 'company_longitude', 'company_radius_m')
        """
    ).fetchall()
    settings = {row["key"]: row["value"] for row in rows}
    return {
        "latitude": float(settings.get("company_latitude", "18.0735")),
        "longitude": float(settings.get("company_longitude", "-15.9582")),
        "radiusMeters": float(settings.get("company_radius_m", "150")),
    }


@app.before_request
def ensure_database_ready():
    global DB_INITIALIZED
    if DB_INITIALIZED or request.endpoint in {"health", "health_db", "setup_init_db"}:
        return
    init_db()
    DB_INITIALIZED = True


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/health/db")
def health_db():
    try:
        init_db()
        with db_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return jsonify({"ok": True})
    except Exception as error:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": error.__class__.__name__,
                    "message": str(error),
                    "databaseUrlConfigured": bool(os.getenv("DATABASE_URL")),
                }
            ),
            500,
        )


@app.post("/setup/init-db")
def setup_init_db():
    expected = os.getenv("SETUP_TOKEN")
    provided = request.headers.get("X-Setup-Token") or request.args.get("token")
    if not expected or provided != expected:
        return jsonify({"ok": False, "reason": "forbidden"}), 403
    init_db()
    return jsonify({"ok": True})


@app.post("/auth/request-otp")
def request_otp():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    purpose = data.get("purpose") or "login"
    if not email:
        return jsonify({"ok": False, "reason": "email_required"}), 400
    with db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE email = %s",
            (email,),
        ).fetchone()
        if purpose == "register" and existing is not None:
            return jsonify({"ok": False, "reason": "email_exists"}), 409
        if purpose != "register" and existing is None:
            return jsonify({"ok": False, "reason": "not_found"}), 404
        code = str(100000 + random.randint(0, 899999))
        conn.execute(
            """
            INSERT INTO email_otps (email, code, expires_at, used)
            VALUES (%s, %s, %s, false)
            """,
            (email, code, datetime.now() + timedelta(minutes=10)),
        )
    return jsonify({"ok": True, "code": code})


@app.post("/auth/verify-otp")
def verify_otp():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    code = (data.get("code") or "").strip()
    if not email or not code:
        return jsonify({"ok": False, "reason": "invalid"}), 400
    with db_conn() as conn:
        otp = conn.execute(
            """
            SELECT id, expires_at
              FROM email_otps
             WHERE email = %s AND code = %s AND used = false
             ORDER BY id DESC
             LIMIT 1
            """,
            (email, code),
        ).fetchone()
        if otp is None:
            return jsonify({"ok": False, "reason": "invalid"}), 400
        if datetime.now(otp["expires_at"].tzinfo) > otp["expires_at"]:
            return jsonify({"ok": False, "reason": "expired"}), 400
        conn.execute("UPDATE email_otps SET used = true WHERE id = %s", (otp["id"],))
    return jsonify({"ok": True})


@app.post("/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    full_name = (data.get("fullName") or data.get("full_name") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or "Temp1234"
    phone = (data.get("phone") or "").strip()
    job_title = (data.get("jobTitle") or data.get("job_title") or "").strip()
    role = data.get("role") or "worker"
    status = data.get("status") or "pending"
    otp = (data.get("otp") or "").strip()
    if not full_name or not email or not password:
        return jsonify({"ok": False, "reason": "required"}), 400
    with db_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
        if existing is not None:
            return jsonify({"ok": False, "reason": "email_exists"}), 409
        if otp:
            otp_row = conn.execute(
                """
                SELECT id, expires_at
                  FROM email_otps
                 WHERE email = %s AND code = %s AND used = false
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (email, otp),
            ).fetchone()
            if otp_row is None:
                return jsonify({"ok": False, "reason": "otp_invalid"}), 400
            conn.execute("UPDATE email_otps SET used = true WHERE id = %s", (otp_row["id"],))
        conn.execute(
            """
            INSERT INTO users
              (full_name, email, password_hash, phone, job_title, role, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (full_name, email, generate_password_hash(password), phone, job_title, role, status),
        )
    return jsonify({"ok": True})


@app.post("/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    with db_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
    if user is None:
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if user["status"] != "approved":
        return jsonify({"ok": False, "reason": user["status"]}), 403
    if not password_matches(user["password_hash"], password):
        return jsonify({"ok": False, "reason": "invalid"}), 401
    return jsonify({"ok": True, "user": user_payload(user)})


@app.post("/auth/change-password")
def change_password():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    old_password = data.get("oldPassword") or data.get("old_password") or ""
    new_password = data.get("newPassword") or data.get("new_password") or ""
    if not email or not old_password or not new_password:
        return jsonify({"ok": False, "reason": "required"}), 400
    with db_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        if user is None:
            return jsonify({"ok": False, "reason": "not_found"}), 404
        if not password_matches(user["password_hash"], old_password):
            return jsonify({"ok": False, "reason": "invalid"}), 401
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (generate_password_hash(new_password), user["id"]),
        )
    return jsonify({"ok": True})


@app.post("/attendance/scan")
def scan_attendance():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    token = (data.get("token") or "").strip()
    with db_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        if user is None:
            return jsonify({"ok": False, "reason": "not_found"}), 404
        if user["status"] != "approved":
            return jsonify({"ok": False, "reason": user["status"]}), 403
        date = today_date()
        existing = conn.execute(
            "SELECT id FROM attendance WHERE user_id = %s AND attend_date = %s",
            (user["id"], date),
        ).fetchone()
        if existing is not None:
            return jsonify({"ok": True, "already": True})
        if token != today_token():
            conn.execute(
                """
                INSERT INTO attendance (user_id, attend_date, status, verification_reason)
                VALUES (%s, %s, 'absent', 'invalid_qr')
                """,
                (user["id"], date),
            )
            return jsonify({"ok": False, "reason": "invalid_qr"}), 400
        conn.execute(
            """
            INSERT INTO attendance (user_id, attend_date, status, time, verification_reason)
            VALUES (%s, %s, 'present', %s, 'qr')
            """,
            (user["id"], date, now_time()),
        )
    return jsonify({"ok": True, "already": False})


@app.post("/attendance/location")
def location_attendance():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "invalid_location"}), 400
    with db_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        if user is None:
            return jsonify({"ok": False, "reason": "not_found"}), 404
        if user["status"] != "approved":
            return jsonify({"ok": False, "reason": user["status"]}), 403
        company = get_company_location(conn)
        distance_m = distance_meters(
            company["latitude"],
            company["longitude"],
            latitude,
            longitude,
        )
        verified = distance_m <= company["radiusMeters"]
        reason = "verified" if verified else "outside_area"
        date = today_date()
        existing = conn.execute(
            "SELECT id FROM attendance WHERE user_id = %s AND attend_date = %s",
            (user["id"], date),
        ).fetchone()
        if existing is not None:
            return jsonify(
                {
                    "ok": True,
                    "already": True,
                    "verified": verified,
                    "distanceMeters": distance_m,
                    "allowedRadiusMeters": company["radiusMeters"],
                }
            )
        conn.execute(
            """
            INSERT INTO attendance
              (user_id, attend_date, status, time, latitude, longitude, distance_m, verification_reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user["id"],
                date,
                "present" if verified else "absent",
                now_time(),
                latitude,
                longitude,
                distance_m,
                reason,
            ),
        )
    return jsonify(
        {
            "ok": True,
            "already": False,
            "verified": verified,
            "distanceMeters": distance_m,
            "allowedRadiusMeters": company["radiusMeters"],
            "reason": reason,
        }
    )


@app.get("/attendance/list")
def attendance_list():
    month = request.args.get("month") or datetime.now().strftime("%Y-%m")
    group = request.args.get("group") or ""
    params = [month]
    group_filter = ""
    if group and group != "Tous":
        group_filter = "AND u.job_title = %s"
        params.append(group)
    with db_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT a.attend_date::text AS date,
                   a.status,
                   a.time,
                   a.latitude,
                   a.longitude,
                   a.distance_m,
                   a.verification_reason,
                   u.full_name AS name,
                   u.job_title AS "group"
              FROM attendance a
              JOIN users u ON a.user_id = u.id
             WHERE to_char(a.attend_date, 'YYYY-MM') = %s
             {group_filter}
             ORDER BY a.attend_date DESC, a.id DESC
            """,
            params,
        ).fetchall()
    return jsonify({"items": rows})


@app.get("/attendance/report")
def attendance_report():
    month = request.args.get("month") or datetime.now().strftime("%Y-%m")
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.full_name AS name,
                   COALESCE(SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END), 0)::int AS present,
                   COALESCE(SUM(CASE WHEN a.status IS NOT NULL AND a.status <> 'present' THEN 1 ELSE 0 END), 0)::int AS absent
              FROM users u
              LEFT JOIN attendance a
                ON a.user_id = u.id
               AND to_char(a.attend_date, 'YYYY-MM') = %s
             WHERE u.role = 'worker'
             GROUP BY u.id
             ORDER BY lower(u.full_name)
            """,
            (month,),
        ).fetchall()
    return jsonify({"items": rows})


@app.get("/admin/users")
def list_users():
    status = request.args.get("status") or "pending"
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE status = %s ORDER BY id DESC",
            (status,),
        ).fetchall()
    return jsonify({"users": [user_payload(row) for row in rows]})


@app.post("/admin/users/<int:user_id>/approve")
def approve_user(user_id):
    with db_conn() as conn:
        conn.execute("UPDATE users SET status = 'approved' WHERE id = %s", (user_id,))
    return jsonify({"ok": True})


@app.post("/admin/users/<int:user_id>/reject")
def reject_user(user_id):
    with db_conn() as conn:
        conn.execute("UPDATE users SET status = 'rejected' WHERE id = %s", (user_id,))
    return jsonify({"ok": True})


@app.get("/qr/today")
def qr_today():
    return jsonify({"token": today_token()})


@app.get("/company-location")
def company_location():
    with db_conn() as conn:
        location = get_company_location(conn)
    return jsonify({"ok": True, "location": location})


@app.put("/company-location")
def update_company_location():
    data = request.get_json(silent=True) or {}
    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
        radius_m = float(data.get("radiusMeters", data.get("radius_m", 150)))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "invalid"}), 400
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180) or radius_m <= 0:
        return jsonify({"ok": False, "reason": "invalid"}), 400
    with db_conn() as conn:
        values = {
            "company_latitude": str(latitude),
            "company_longitude": str(longitude),
            "company_radius_m": str(radius_m),
        }
        for key, value in values.items():
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE SET
                  value = EXCLUDED.value,
                  updated_at = now()
                """,
                (key, value),
            )
        location = get_company_location(conn)
    return jsonify({"ok": True, "location": location})


@app.post("/locations")
def add_location():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    url = (data.get("url") or "").strip()
    if not email or not url:
        return jsonify({"ok": False, "reason": "required"}), 400
    with db_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO locations (email, url)
            VALUES (%s, %s)
            RETURNING id, email, url, created_at
            """,
            (email, url),
        ).fetchone()
    row["created_at"] = row["created_at"].isoformat()
    return jsonify({"ok": True, "location": row})


@app.get("/locations")
def list_locations():
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id, email, url, created_at FROM locations ORDER BY id DESC"
        ).fetchall()
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
    return jsonify({"items": rows})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-db", action="store_true")
    args = parser.parse_args()
    if args.init_db:
        init_db()
        print("Database initialized.")
        return
    init_db()
    port = int(os.getenv("PORT", "5000"))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.getenv("FLASK_DEBUG") == "1",
        load_dotenv=False,
    )


if __name__ == "__main__":
    main()

import argparse
import os
import random
import smtplib
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from email.message import EmailMessage
from math import asin, cos, radians, sin, sqrt
from urllib.parse import parse_qs, urlparse, urlunparse

import psycopg
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
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
app.secret_key = os.getenv("SECRET_KEY") or os.getenv("SETUP_TOKEN") or "codeva-dev-secret"
CORS(app)
DB_INITIALIZED = False
PAGES_DIR = os.path.join(os.path.dirname(__file__), "static", "pages")
DEV_EMAIL = os.getenv("DEV_EMAIL", "codeva@gmail.com")
DEV_PASSWORD = os.getenv("DEV_PASSWORD", "codeva123")
MANAGER_ROLES = {"admin", "manager", "company_manager", "company_admin"}
ATTENDANCE_QR_TOKEN = os.getenv("ATTENDANCE_QR_TOKEN", "codeva-presence-checkin")


def database_connect_attempts():
    parsed = urlparse(DATABASE_URL)
    host = parsed.hostname or ""
    query = parse_qs(parsed.query)
    has_sslmode = "sslmode" in query
    base_kwargs = {"row_factory": dict_row, "connect_timeout": 10}
    attempts = [(DATABASE_URL, dict(base_kwargs))]

    if not has_sslmode and host not in {"localhost", "127.0.0.1", "::1"}:
        disable_kwargs = dict(base_kwargs)
        disable_kwargs["sslmode"] = "disable"
        attempts.append((DATABASE_URL, disable_kwargs))

        require_kwargs = dict(base_kwargs)
        require_kwargs["sslmode"] = "require"
        attempts.append((DATABASE_URL, require_kwargs))

    if host.endswith(".render.com") and "-postgres.render.com" in host:
        internal_host = host.split(".", 1)[0]
        internal_netloc = parsed.netloc.replace(host, internal_host, 1)
        internal_url = urlunparse(parsed._replace(netloc=internal_netloc, query=""))
        internal_kwargs = dict(base_kwargs)
        internal_kwargs["sslmode"] = "disable"
        attempts.append((internal_url, internal_kwargs))
        attempts.append((internal_url, dict(base_kwargs)))

    unique_attempts = []
    seen = set()
    for conninfo, kwargs in attempts:
        key = (conninfo, tuple(sorted(kwargs.items())))
        if key not in seen:
            seen.add(key)
            unique_attempts.append((conninfo, kwargs))
    return unique_attempts


@contextmanager
def db_conn():
    attempts = database_connect_attempts()
    for retry in range(2):
        for index, (conninfo, kwargs) in enumerate(attempts):
            try:
                conn = psycopg.connect(conninfo, **kwargs)
                break
            except psycopg.OperationalError:
                if retry == 1 and index == len(attempts) - 1:
                    raise
        else:
            time.sleep(0.5 * (retry + 1))
            continue
        break
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def attendance_link():
    return url_for("worker_checkin_page", _external=True)


def token_from_qr(value):
    value = (value or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        query = parse_qs(parsed.query)
        if query.get("token"):
            return query["token"][0].strip()
        return parsed.path.rstrip("/").rsplit("/", 1)[-1].strip()
    return value


def is_attendance_qr(value):
    raw_value = (value or "").strip()
    if not raw_value:
        return False
    parsed = urlparse(raw_value)
    if parsed.scheme and parsed.netloc:
        return parsed.path.rstrip("/") == url_for("worker_checkin_page").rstrip("/")
    return raw_value == ATTENDANCE_QR_TOKEN


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


def user_is_manager(user):
    return (user.get("role") or "").strip().lower() in MANAGER_ROLES


def smtp_configured():
    return all(
        os.getenv(key, "").strip()
        for key in ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"]
    )


def send_otp_email(email, code):
    if not smtp_configured():
        if os.getenv("FLASK_DEBUG") == "1":
            return False
        raise RuntimeError("SMTP is not configured")

    message = EmailMessage()
    message["Subject"] = "Code de verification CODEVA"
    message["From"] = os.getenv("SMTP_FROM", "").strip()
    message["To"] = email
    message.set_content(
        "\n".join(
            [
                "Bonjour,",
                "",
                f"Votre code de verification est: {code}",
                "Ce code expire dans 10 minutes.",
                "",
                "Si vous n'avez pas demande ce code, ignorez ce message.",
            ]
        )
    )

    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT", "587").strip())
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip().replace(" ", "")
    use_ssl = os.getenv("SMTP_USE_SSL", "0") == "1"
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=20) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(message)
    return True


def user_payload(user):
    return {
        "id": user["id"],
        "fullName": user["full_name"],
        "email": user["email"],
        "phone": user.get("phone") or "",
        "jobTitle": user.get("job_title") or "",
        "role": user["role"],
        "status": user["status"],
        "managerEmail": user.get("manager_email") or "",
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
      manager_email TEXT DEFAULT '',
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
      purpose TEXT NOT NULL DEFAULT 'login',
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
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS manager_email TEXT DEFAULT ''")
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

        dev_password = generate_password_hash(DEV_PASSWORD)
        dev_user = conn.execute(
            "SELECT id FROM users WHERE email = %s",
            (DEV_EMAIL,),
        ).fetchone()
        if dev_user is None:
            conn.execute(
                """
                INSERT INTO users
                  (full_name, email, password_hash, phone, job_title, role, status)
                VALUES (%s, %s, %s, %s, %s, 'admin', 'approved')
                """,
                ("CODEVA Developer", DEV_EMAIL, dev_password, "", "Developer"),
            )
        else:
            conn.execute(
                """
                UPDATE users
                   SET password_hash = %s,
                       role = 'admin',
                       status = 'approved',
                       full_name = COALESCE(NULLIF(full_name, ''), 'CODEVA Developer'),
                       job_title = COALESCE(NULLIF(job_title, ''), 'Developer')
                 WHERE email = %s
                """,
                (dev_password, DEV_EMAIL),
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
        ensure_attendance_columns(conn)
        ensure_otp_columns(conn)


def ensure_attendance_columns(conn):
    columns = {
        "time": "TEXT",
        "latitude": "DOUBLE PRECISION",
        "longitude": "DOUBLE PRECISION",
        "distance_m": "DOUBLE PRECISION",
        "verification_reason": "TEXT",
    }
    for column, column_type in columns.items():
        conn.execute(f"ALTER TABLE attendance ADD COLUMN IF NOT EXISTS {column} {column_type}")


def ensure_otp_columns(conn):
    conn.execute(
        "ALTER TABLE email_otps ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'login'"
    )
    conn.execute("ALTER TABLE email_otps ALTER COLUMN purpose SET DEFAULT 'login'")


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
    if DB_INITIALIZED or request.endpoint in {
        "health",
        "health_db",
        "setup_init_db",
        "static",
        "web_app",
        "contact_page",
        "about_page",
        "privacy_page",
        "delete_account_page",
        "dev_login_page",
        "dev_login_submit",
        "dev_logout",
        "dev_page",
        "create_dev_admin",
        "freeze_dev_admin",
        "activate_dev_admin",
        "delete_dev_admin",
        "manager_page",
        "manager_workers",
        "create_manager_worker",
        "manager_attendance",
        "manager_weekly_report",
        "manager_logout",
        "worker_page",
        "worker_checkin_page",
        "worker_logout",
        "qr_today",
        "web_manifest",
        "service_worker",
    }:
        return
    ensure_database_initialized()


def ensure_database_initialized():
    global DB_INITIALIZED
    if DB_INITIALIZED:
        return
    init_db()
    DB_INITIALIZED = True


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/")
@app.get("/web")
def web_app():
    return render_template("index.html")


@app.get("/contact")
def contact_page():
    return send_from_directory(PAGES_DIR, "contact.html")


@app.get("/about")
def about_page():
    return send_from_directory(PAGES_DIR, "about.html")


@app.get("/privacy")
def privacy_page():
    return send_from_directory(PAGES_DIR, "privacy.html")


@app.get("/delete-account")
def delete_account_page():
    return send_from_directory(PAGES_DIR, "delete-account.html")


@app.get("/manifest.webmanifest")
def web_manifest():
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "static", "web"),
        "manifest.webmanifest",
        mimetype="application/manifest+json",
    )


@app.get("/service-worker.js")
def service_worker():
    response = send_from_directory(
        os.path.join(os.path.dirname(__file__), "static", "web"),
        "service-worker.js",
        mimetype="text/javascript",
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


def dev_is_authenticated():
    return session.get("dev_authenticated") is True


def require_dev_login():
    if dev_is_authenticated():
        return None
    return redirect(url_for("dev_login_page"))


@app.get("/dev/login")
def dev_login_page():
    if dev_is_authenticated():
        return redirect(url_for("dev_page"))
    return render_template("dev_login.html", error=None)


@app.post("/dev/login")
def dev_login_submit():
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    ensure_database_initialized()
    with db_conn() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE email = %s",
            (email,),
        ).fetchone()
    if (
        user is not None
        and user["email"] == DEV_EMAIL
        and user["status"] == "approved"
        and password_matches(user["password_hash"], password)
    ):
        session["dev_authenticated"] = True
        session["dev_email"] = user["email"]
        return redirect(url_for("dev_page"))
    return render_template("dev_login.html", error="بيانات الدخول غير صحيحة"), 401


@app.post("/dev/logout")
def dev_logout():
    session.pop("dev_authenticated", None)
    session.pop("dev_email", None)
    return redirect(url_for("dev_login_page"))


def manager_is_authenticated():
    return session.get("manager_authenticated") is True


def worker_is_authenticated():
    return session.get("worker_authenticated") is True


def require_manager_login():
    if manager_is_authenticated():
        return None
    return redirect(url_for("web_app"))


def require_worker_login():
    if worker_is_authenticated():
        return None
    return redirect(url_for("web_app"))


@app.post("/manager/logout")
def manager_logout():
    session.pop("manager_authenticated", None)
    session.pop("manager_email", None)
    return redirect(url_for("web_app"))


@app.post("/worker/logout")
def worker_logout():
    session.pop("worker_authenticated", None)
    session.pop("worker_email", None)
    session.pop("worker_name", None)
    return redirect(url_for("web_app"))


@app.get("/manager")
def manager_page():
    auth = require_manager_login()
    if auth:
        return auth
    return render_template("manager.html", manager_email=session.get("manager_email"))


@app.get("/worker")
def worker_page():
    auth = require_worker_login()
    if auth:
        return auth
    return render_template(
        "worker.html",
        worker_email=session.get("worker_email"),
        worker_name=session.get("worker_name"),
    )


@app.get("/worker/checkin")
def worker_checkin_page():
    if not worker_is_authenticated():
        session["worker_next"] = request.full_path
        return redirect(url_for("web_app"))
    status_message = "تعذر تسجيل الحضور."
    status_type = "error"
    ensure_database_initialized()
    with db_conn() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE email = %s",
            (session.get("worker_email"),),
        ).fetchone()
        if user is None:
            status_message = "الحساب غير موجود."
        elif user["status"] != "approved":
            status_message = "الحساب غير مفعل بعد."
        else:
            result, _ = record_qr_attendance(conn, user, validate_qr=False)
            if result.get("ok") and result.get("already"):
                status_message = "تم تسجيل حضورك مسبقا اليوم."
                status_type = "ok"
            elif result.get("ok"):
                status_message = "تم تسجيل حضورك بنجاح."
                status_type = "ok"
            elif result.get("reason") == "invalid_qr":
                status_message = "رابط QR غير صالح."
    return render_template(
        "worker_checkin.html",
        worker_email=session.get("worker_email"),
        worker_name=session.get("worker_name"),
        status_message=status_message,
        status_type=status_type,
    )


@app.get("/manager/workers")
def manager_workers():
    auth = require_manager_login()
    if auth:
        return auth
    return manager_workers_payload()


def manager_workers_payload(message=None, error=None):
    ensure_database_initialized()
    with db_conn() as conn:
        workers = conn.execute(
            """
            SELECT id, full_name, email, phone, job_title, status, created_at
              FROM users
             WHERE role = 'worker'
               AND manager_email = %s
             ORDER BY lower(full_name), id DESC
            """,
            (session.get("manager_email"),),
        ).fetchall()
    return render_template(
        "manager_workers.html",
        workers=workers,
        message=message,
        error=error,
    )


@app.post("/manager/workers")
def create_manager_worker():
    auth = require_manager_login()
    if auth:
        return auth
    ensure_database_initialized()
    full_name = (request.form.get("fullName") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    password = request.form.get("password") or "1234"
    job_title = (request.form.get("jobTitle") or "").strip()
    manager_email = session.get("manager_email") or ""
    if not full_name or not phone or not password:
        return manager_workers_payload(error="اسم العامل والرقم وكلمة المرور مطلوبة"), 400
    safe_phone = "".join(ch for ch in phone if ch.isalnum())
    if not safe_phone:
        return manager_workers_payload(error="رقم العامل غير صالح"), 400
    worker_email = f"{safe_phone}@workers.codeva.local"
    with db_conn() as conn:
        existing_phone = conn.execute(
            "SELECT id FROM users WHERE phone = %s",
            (phone,),
        ).fetchone()
        if existing_phone is not None:
            return manager_workers_payload(error="هذا الرقم مستخدم مسبقا"), 409
        existing_email = conn.execute(
            "SELECT id FROM users WHERE email = %s",
            (worker_email,),
        ).fetchone()
        if existing_email is not None:
            return manager_workers_payload(error="تعذر إنشاء حساب داخلي لهذا الرقم"), 409
        conn.execute(
            """
            INSERT INTO users
              (full_name, email, password_hash, phone, job_title, role, status, manager_email)
            VALUES (%s, %s, %s, %s, %s, 'worker', 'approved', %s)
            """,
            (
                full_name,
                worker_email,
                generate_password_hash(password),
                phone,
                job_title,
                manager_email,
            ),
        )
    return manager_workers_payload(message="تمت إضافة العامل")


@app.get("/manager/attendance")
def manager_attendance():
    auth = require_manager_login()
    if auth:
        return auth
    ensure_database_initialized()
    date = today_date()
    with db_conn() as conn:
        items = conn.execute(
            """
            SELECT u.id,
                   u.full_name,
                   u.email,
                   u.job_title,
                   COALESCE(a.status, 'absent') AS status,
                   COALESCE(a.time, to_char(a.created_at, 'HH24:MI')) AS attendance_time,
                   a.verification_reason,
                   a.distance_m
              FROM users u
             LEFT JOIN attendance a
                ON a.user_id = u.id
               AND a.attend_date = %s
             WHERE u.role = 'worker'
               AND u.manager_email = %s
             ORDER BY lower(u.full_name), u.id DESC
            """,
            (date, session.get("manager_email")),
        ).fetchall()
    present_count = sum(1 for item in items if item["status"] == "present")
    absent_count = len(items) - present_count
    return render_template(
        "manager_attendance.html",
        items=items,
        date=date,
        present_count=present_count,
        absent_count=absent_count,
    )


@app.get("/manager/report")
def manager_weekly_report():
    auth = require_manager_login()
    if auth:
        return auth
    ensure_database_initialized()
    today = datetime.now().date()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    elapsed_days = (min(today, end) - start).days + 1
    with db_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.id,
                   u.full_name,
                   u.email,
                   u.job_title,
                   COALESCE(SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END), 0)::int AS present
              FROM users u
             LEFT JOIN attendance a
                ON a.user_id = u.id
               AND a.attend_date BETWEEN %s AND %s
             WHERE u.role = 'worker'
               AND u.manager_email = %s
             GROUP BY u.id
             ORDER BY lower(u.full_name), u.id DESC
            """,
            (start, end, session.get("manager_email")),
        ).fetchall()
    report = [
        {
            "name": row["full_name"],
            "email": row["email"],
            "job": row["job_title"],
            "present": row["present"],
            "absent": max(elapsed_days - row["present"], 0),
        }
        for row in rows
    ]
    return render_template(
        "manager_report.html",
        report=report,
        start=start.isoformat(),
        end=end.isoformat(),
        elapsed_days=elapsed_days,
    )


def dev_admins_payload(message=None, error=None):
    ensure_database_initialized()
    with db_conn() as conn:
        admins = conn.execute(
            """
            SELECT id, full_name, email, phone, job_title, status, created_at
              FROM users
             WHERE role = 'admin'
               AND email <> %s
             ORDER BY id DESC
            """,
            (DEV_EMAIL,),
        ).fetchall()
    return render_template(
        "dev.html",
        admins=admins,
        message=message,
        error=error,
    )


@app.get("/dev")
def dev_page():
    auth = require_dev_login()
    if auth:
        return auth
    return dev_admins_payload()


@app.post("/dev/admins")
def create_dev_admin():
    auth = require_dev_login()
    if auth:
        return auth
    ensure_database_initialized()
    full_name = (request.form.get("fullName") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or "Temp1234"
    phone = (request.form.get("phone") or "").strip()
    company_name = (request.form.get("companyName") or "").strip()
    if not full_name or not email or not password:
        return dev_admins_payload(error="الاسم والبريد وكلمة المرور مطلوبة"), 400
    if email == DEV_EMAIL:
        return dev_admins_payload(error="هذا البريد محجوز لحساب المطور"), 400
    with db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE email = %s",
            (email,),
        ).fetchone()
        if existing is not None:
            return dev_admins_payload(error="هذا البريد مستخدم مسبقا"), 409
        conn.execute(
            """
            INSERT INTO users
              (full_name, email, password_hash, phone, job_title, role, status)
            VALUES (%s, %s, %s, %s, %s, 'admin', 'approved')
            """,
            (
                full_name,
                email,
                generate_password_hash(password),
                phone,
                company_name,
            ),
        )
    return dev_admins_payload(message="تمت إضافة مدير الشركة")


@app.post("/dev/admins/<int:user_id>/freeze")
def freeze_dev_admin(user_id):
    auth = require_dev_login()
    if auth:
        return auth
    ensure_database_initialized()
    with db_conn() as conn:
        conn.execute(
            "UPDATE users SET status = 'frozen' WHERE id = %s AND role = 'admin'",
            (user_id,),
        )
    return dev_admins_payload(message="تم تجميد الحساب")


@app.post("/dev/admins/<int:user_id>/activate")
def activate_dev_admin(user_id):
    auth = require_dev_login()
    if auth:
        return auth
    ensure_database_initialized()
    with db_conn() as conn:
        conn.execute(
            "UPDATE users SET status = 'approved' WHERE id = %s AND role = 'admin'",
            (user_id,),
        )
    return dev_admins_payload(message="تم تفعيل الحساب")


@app.post("/dev/admins/<int:user_id>/delete")
def delete_dev_admin(user_id):
    auth = require_dev_login()
    if auth:
        return auth
    ensure_database_initialized()
    with db_conn() as conn:
        conn.execute(
            "DELETE FROM users WHERE id = %s AND role = 'admin'",
            (user_id,),
        )
    return dev_admins_payload(message="تم حذف الحساب")


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


@app.get("/health/email")
def health_email():
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = os.getenv("SMTP_PORT", "").strip()
    return jsonify(
        {
            "ok": True,
            "smtpConfigured": smtp_configured(),
            "hostConfigured": bool(os.getenv("SMTP_HOST", "").strip()),
            "portConfigured": bool(os.getenv("SMTP_PORT", "").strip()),
            "userConfigured": bool(os.getenv("SMTP_USER", "").strip()),
            "passwordConfigured": bool(os.getenv("SMTP_PASSWORD", "").strip()),
            "fromConfigured": bool(os.getenv("SMTP_FROM", "").strip()),
            "useSsl": os.getenv("SMTP_USE_SSL", "0").strip() == "1",
            "smtpHost": smtp_host,
            "smtpPort": smtp_port,
        }
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
            INSERT INTO email_otps (email, code, purpose, expires_at, used, created_at)
            VALUES (%s, %s, %s, %s, false, %s)
            """,
            (
                email,
                code,
                purpose,
                datetime.now() + timedelta(minutes=10),
                datetime.now(),
            ),
        )
    try:
        sent = send_otp_email(email, code)
    except Exception as error:
        app.logger.exception("Failed to send OTP email")
        return (
            jsonify(
                {
                    "ok": False,
                    "reason": "email_send_failed",
                    "error": error.__class__.__name__,
                }
            ),
            500,
        )
    response = {"ok": True, "sent": sent}
    if os.getenv("FLASK_DEBUG") == "1":
        response["code"] = code
    return jsonify(response)


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
    is_form_request = bool(request.form) and not request.is_json
    data = request.form if is_form_request else (request.get_json(silent=True) or {})
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    with db_conn() as conn:
        user = conn.execute(
            """
            SELECT *
              FROM users
             WHERE email = %s OR phone = %s
             ORDER BY id DESC
             LIMIT 1
            """,
            (email, email),
        ).fetchone()
    if user is None:
        if is_form_request:
            return login_result_page("بيانات الدخول غير صحيحة", ok=False), 404
        return jsonify({"ok": False, "reason": "not_found"}), 404
    if user["status"] != "approved":
        if is_form_request:
            return login_result_page("الحساب غير مفعل بعد", ok=False), 403
        return jsonify({"ok": False, "reason": user["status"]}), 403
    if not password_matches(user["password_hash"], password):
        if is_form_request:
            return login_result_page("بيانات الدخول غير صحيحة", ok=False), 401
        return jsonify({"ok": False, "reason": "invalid"}), 401
    if user["email"] == DEV_EMAIL:
        session["dev_authenticated"] = True
        session["dev_email"] = user["email"]
        if is_form_request:
            return redirect(url_for("dev_page"))
        return jsonify({"ok": True, "user": user_payload(user), "redirect": url_for("dev_page")})
    if user_is_manager(user):
        session["manager_authenticated"] = True
        session["manager_email"] = user["email"]
        if is_form_request:
            return redirect(url_for("manager_page"))
        return jsonify({"ok": True, "user": user_payload(user), "redirect": url_for("manager_page")})
    session["worker_authenticated"] = True
    session["worker_email"] = user["email"]
    session["worker_name"] = user["full_name"]
    if is_form_request:
        next_url = session.pop("worker_next", None)
        return redirect(next_url or url_for("worker_page"))
    return jsonify({"ok": True, "user": user_payload(user), "redirect": url_for("worker_page")})


def login_result_page(message, ok):
    color = "#166534" if ok else "#991b1b"
    background = "#dcfce7" if ok else "#fee2e2"
    return f"""
    <!doctype html>
    <html lang="ar" dir="rtl">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>CODEVA Presence</title>
      </head>
      <body style="margin:0;font-family:Tahoma,Arial,sans-serif;background:#f5f8f7;color:#172033;display:grid;place-items:center;min-height:100vh;">
        <main style="width:min(100% - 32px,430px);background:#fff;border:1px solid #e4e7ec;border-radius:8px;padding:24px;text-align:center;">
          <h1 style="margin:0 0 12px;color:#255b48;">CODEVA Presence</h1>
          <p style="margin:0 0 18px;padding:12px;border-radius:8px;color:{color};background:{background};">{message}</p>
          <a href="/web" style="display:inline-flex;align-items:center;justify-content:center;min-height:42px;padding:0 16px;border-radius:8px;background:#255b48;color:#fff;text-decoration:none;font-weight:700;">رجوع</a>
        </main>
      </body>
    </html>
    """


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


@app.post("/auth/reset-password")
def reset_password():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    code = (data.get("code") or "").strip()
    new_password = data.get("newPassword") or data.get("new_password") or ""
    if not email or not code or not new_password:
        return jsonify({"ok": False, "reason": "required"}), 400
    if len(new_password) < 4:
        return jsonify({"ok": False, "reason": "weak_password"}), 400
    with db_conn() as conn:
        user = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
        if user is None:
            return jsonify({"ok": False, "reason": "not_found"}), 404
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
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (generate_password_hash(new_password), user["id"]),
        )
    return jsonify({"ok": True})


@app.post("/attendance/scan")
def scan_attendance():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    with db_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        if user is None:
            return jsonify({"ok": False, "reason": "not_found"}), 404
        if user["status"] != "approved":
            return jsonify({"ok": False, "reason": user["status"]}), 403
        result, status_code = record_qr_attendance(conn, user, validate_qr=False)
    return jsonify(result), status_code


def record_qr_attendance(conn, user, qr_value=None, validate_qr=True):
    date = today_date()
    existing = conn.execute(
        "SELECT id, status, time FROM attendance WHERE user_id = %s AND attend_date = %s",
        (user["id"], date),
    ).fetchone()
    if validate_qr and not is_attendance_qr(qr_value):
        if existing is None:
            conn.execute(
                """
                INSERT INTO attendance (user_id, attend_date, status, verification_reason)
                VALUES (%s, %s, 'absent', 'invalid_qr')
                """,
                (user["id"], date),
            )
        return {"ok": False, "reason": "invalid_qr"}, 400
    if existing is not None:
        if existing["status"] != "present" or not existing["time"]:
            conn.execute(
                """
                UPDATE attendance
                   SET status = 'present',
                       time = COALESCE(time, %s),
                       verification_reason = 'qr'
                 WHERE id = %s
                """,
                (now_time(), existing["id"]),
            )
            return {"ok": True, "already": False, "updated": True}, 200
        return {"ok": True, "already": True}, 200
    conn.execute(
        """
        INSERT INTO attendance (user_id, attend_date, status, time, verification_reason)
        VALUES (%s, %s, 'present', %s, 'qr')
        """,
        (user["id"], date, now_time()),
    )
    return {"ok": True, "already": False}, 200


def record_location_attendance(email, latitude, longitude, token=None):
    if token is not None and not is_attendance_qr(token):
        return jsonify({"ok": False, "reason": "invalid_qr"}), 400
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
        reason = "qr_gps" if token is not None and verified else "verified" if verified else "outside_area"
        date = today_date()
        existing = conn.execute(
            "SELECT id, status, time FROM attendance WHERE user_id = %s AND attend_date = %s",
            (user["id"], date),
        ).fetchone()
        if existing is not None:
            if verified and (existing["status"] != "present" or not existing["time"]):
                attendance_time = now_time()
                conn.execute(
                    """
                    UPDATE attendance
                       SET status = 'present',
                           time = COALESCE(time, %s),
                           latitude = %s,
                           longitude = %s,
                           distance_m = %s,
                           verification_reason = %s
                     WHERE id = %s
                    """,
                    (
                        attendance_time,
                        latitude,
                        longitude,
                        distance_m,
                        reason,
                        existing["id"],
                    ),
                )
                return jsonify(
                    {
                        "ok": True,
                        "already": False,
                        "updated": True,
                        "verified": True,
                        "distanceMeters": distance_m,
                        "allowedRadiusMeters": company["radiusMeters"],
                        "time": attendance_time,
                    }
                )
            return jsonify(
                {
                    "ok": True,
                    "already": True,
                    "verified": verified,
                    "distanceMeters": distance_m,
                    "allowedRadiusMeters": company["radiusMeters"],
                    "status": existing["status"],
                    "time": existing["time"],
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


@app.post("/attendance/gps-qr")
def gps_qr_attendance():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or session.get("worker_email") or "").strip()
    token = data.get("token")
    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "invalid_location"}), 400
    return record_location_attendance(email, latitude, longitude, token=token)


@app.post("/attendance/location")
def location_attendance():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "invalid_location"}), 400
    return record_location_attendance(email, latitude, longitude)


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
    return jsonify({"token": ATTENDANCE_QR_TOKEN, "url": attendance_link()})


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

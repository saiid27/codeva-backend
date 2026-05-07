import argparse
import os
import sqlite3
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash

import app


DATABASE_URL = os.getenv("DATABASE_URL", app.DATABASE_URL)


def table_exists(sqlite_conn, table):
    row = sqlite_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def sqlite_columns(sqlite_conn, table):
    return {row["name"] for row in sqlite_conn.execute(f"PRAGMA table_info({table})")}


def value(row, key, default=None):
    if key in row.keys():
        return row[key]
    return default


def parse_sqlite_datetime(raw):
    if raw is None:
        return datetime.now(timezone.utc)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000, timezone.utc)
    text = str(raw).strip()
    if not text:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return datetime.now(timezone.utc)


def migrate_users(sqlite_conn, pg_conn):
    if not table_exists(sqlite_conn, "users"):
        return 0
    rows = sqlite_conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    count = 0
    for row in rows:
        password = value(row, "password_hash", "") or "Temp1234"
        if not str(password).startswith(("pbkdf2:", "scrypt:")):
            password = generate_password_hash(str(password))
        pg_conn.execute(
            """
            INSERT INTO users
              (full_name, email, password_hash, phone, job_title, role, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET
              full_name = EXCLUDED.full_name,
              password_hash = EXCLUDED.password_hash,
              phone = EXCLUDED.phone,
              job_title = EXCLUDED.job_title,
              role = EXCLUDED.role,
              status = EXCLUDED.status
            """,
            (
                value(row, "full_name", ""),
                value(row, "email", ""),
                password,
                value(row, "phone", "") or "",
                value(row, "job_title", "") or "",
                value(row, "role", "worker") or "worker",
                value(row, "status", "pending") or "pending",
            ),
        )
        count += 1
    return count


def migrate_attendance(sqlite_conn, pg_conn):
    if not table_exists(sqlite_conn, "attendance"):
        return 0
    rows = sqlite_conn.execute(
        """
        SELECT a.*, u.email
          FROM attendance a
          JOIN users u ON a.user_id = u.id
         ORDER BY a.id
        """
    ).fetchall()
    count = 0
    for row in rows:
        user = pg_conn.execute(
            "SELECT id FROM users WHERE email = %s",
            (value(row, "email"),),
        ).fetchone()
        if user is None:
            continue
        pg_conn.execute(
            """
            INSERT INTO attendance
              (user_id, attend_date, status, time, latitude, longitude, distance_m, verification_reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id, attend_date) DO UPDATE SET
              status = EXCLUDED.status,
              time = EXCLUDED.time,
              latitude = EXCLUDED.latitude,
              longitude = EXCLUDED.longitude,
              distance_m = EXCLUDED.distance_m,
              verification_reason = EXCLUDED.verification_reason
            """,
            (
                user["id"],
                value(row, "attend_date"),
                value(row, "status", "absent") or "absent",
                value(row, "time"),
                value(row, "latitude"),
                value(row, "longitude"),
                value(row, "distance_m"),
                value(row, "verification_reason"),
            ),
        )
        count += 1
    return count


def migrate_otps(sqlite_conn, pg_conn):
    if not table_exists(sqlite_conn, "email_otps"):
        return 0
    rows = sqlite_conn.execute("SELECT * FROM email_otps ORDER BY id").fetchall()
    count = 0
    for row in rows:
        pg_conn.execute(
            """
            INSERT INTO email_otps (email, code, expires_at, used)
            VALUES (%s, %s, %s, %s)
            """,
            (
                value(row, "email", ""),
                value(row, "code", ""),
                parse_sqlite_datetime(value(row, "expires_at")),
                bool(value(row, "used", 0)),
            ),
        )
        count += 1
    return count


def migrate_locations(sqlite_conn, pg_conn):
    if not table_exists(sqlite_conn, "locations"):
        return 0
    rows = sqlite_conn.execute("SELECT * FROM locations ORDER BY id").fetchall()
    count = 0
    for row in rows:
        pg_conn.execute(
            """
            INSERT INTO locations (email, url, created_at)
            VALUES (%s, %s, %s)
            """,
            (
                value(row, "email", ""),
                value(row, "url", ""),
                parse_sqlite_datetime(value(row, "created_at")),
            ),
        )
        count += 1
    return count


def migrate(sqlite_path):
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(sqlite_path)
    app.init_db()
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    try:
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as pg_conn:
            users = migrate_users(sqlite_conn, pg_conn)
            attendance = migrate_attendance(sqlite_conn, pg_conn)
            otps = migrate_otps(sqlite_conn, pg_conn)
            locations = migrate_locations(sqlite_conn, pg_conn)
            pg_conn.commit()
    finally:
        sqlite_conn.close()
    print(f"users={users}")
    print(f"attendance={attendance}")
    print(f"email_otps={otps}")
    print(f"locations={locations}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sqlite_path", help="Path to presence_qr_local.db")
    args = parser.parse_args()
    migrate(args.sqlite_path)


if __name__ == "__main__":
    main()

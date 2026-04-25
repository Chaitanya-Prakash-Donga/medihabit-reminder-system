"""
=============================================================
  MediHabit Reminder System - Full Backend
  Stack : Python 3.10+ | SQLite | smtplib | pyttsx3 | APScheduler
  No third-party email API (Resend, SendGrid, etc.)
  All email sent via Python's built-in smtplib (SMTP/TLS)
=============================================================
"""

# ─────────────────────────────────────────────────────────────
# 0.  IMPORTS & CONFIGURATION
# ─────────────────────────────────────────────────────────────
import sqlite3
import hashlib
import secrets
import smtplib
import re
import json
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, time as dtime
from typing import Optional

# Voice reminder (text-to-speech – no third-party API)
try:
    import pyttsx3
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False

# APScheduler for background scheduling
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False

# ── SMTP configuration (use your own SMTP server / Gmail) ──
SMTP_CONFIG = {
    "host": "smtp.gmail.com",          # change to your SMTP host
    "port": 587,
    "username": "youremail@gmail.com", # sender email
    "password": "your_app_password",   # Gmail App Password (not account password)
    "from_name": "MediHabit",
}

DB_PATH = "medihabit.db"


# ─────────────────────────────────────────────────────────────
# 1.  DATABASE LAYER
# ─────────────────────────────────────────────────────────────
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create all tables if they do not exist."""
    ddl = """
    -- Users table
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL,
        email       TEXT    NOT NULL UNIQUE,
        phone       TEXT,
        password_hash TEXT  NOT NULL,
        salt        TEXT    NOT NULL,
        timezone    TEXT    DEFAULT 'Asia/Kolkata',
        is_active   INTEGER DEFAULT 1,
        created_at  TEXT    DEFAULT (datetime('now')),
        updated_at  TEXT    DEFAULT (datetime('now'))
    );

    -- Medicines table
    CREATE TABLE IF NOT EXISTS medicines (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name            TEXT    NOT NULL,
        dosage          TEXT    NOT NULL,          -- e.g. "500 mg"
        form            TEXT    DEFAULT 'Tablet',  -- Tablet | Capsule | Syrup | Injection
        frequency       TEXT    NOT NULL,          -- e.g. "Once daily" | "Twice daily"
        times_of_day    TEXT    NOT NULL,          -- JSON array e.g. ["08:00","20:00"]
        start_date      TEXT    NOT NULL,          -- YYYY-MM-DD
        end_date        TEXT,                      -- YYYY-MM-DD or NULL (ongoing)
        stock_count     INTEGER DEFAULT 0,
        low_stock_alert INTEGER DEFAULT 5,         -- alert when stock <= this
        notes           TEXT,
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT    DEFAULT (datetime('now')),
        updated_at      TEXT    DEFAULT (datetime('now'))
    );

    -- Reminder logs table
    CREATE TABLE IF NOT EXISTS reminder_logs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
        scheduled_at TEXT   NOT NULL,
        channel     TEXT    NOT NULL,   -- email | voice | push
        status      TEXT    DEFAULT 'pending',  -- pending | sent | failed | taken | skipped
        sent_at     TEXT,
        taken_at    TEXT,
        created_at  TEXT    DEFAULT (datetime('now'))
    );

    -- Adherence records
    CREATE TABLE IF NOT EXISTS adherence (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
        scheduled_date TEXT  NOT NULL,   -- YYYY-MM-DD
        scheduled_time TEXT  NOT NULL,   -- HH:MM
        status      TEXT    DEFAULT 'pending', -- taken | skipped | missed
        marked_at   TEXT,
        notes       TEXT
    );

    -- Password reset tokens
    CREATE TABLE IF NOT EXISTS reset_tokens (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token       TEXT    NOT NULL UNIQUE,
        expires_at  TEXT    NOT NULL,
        used        INTEGER DEFAULT 0
    );
    """
    with get_connection() as conn:
        conn.executescript(ddl)
    print("[DB] Database initialised.")


# ─────────────────────────────────────────────────────────────
# 2.  UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────
def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode()).hexdigest()


def generate_salt() -> str:
    return secrets.token_hex(16)


def validate_email(email: str) -> bool:
    return bool(re.match(r"^[\w\.\+\-]+@[\w\-]+\.[a-zA-Z]{2,}$", email))


def validate_password(password: str) -> tuple[bool, str]:
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if not re.search(r"[A-Z]", password):
        return False, "Password must contain at least one uppercase letter."
    if not re.search(r"\d", password):
        return False, "Password must contain at least one digit."
    return True, "OK"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────
# 3.  EMAIL SERVICE  (pure smtplib – no third-party API)
# ─────────────────────────────────────────────────────────────
class EmailService:
    """Send emails using Python's built-in smtplib over SMTP/TLS."""

    @staticmethod
    def _send(to_email: str, subject: str, html_body: str) -> bool:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"]    = f"{SMTP_CONFIG['from_name']} <{SMTP_CONFIG['username']}>"
            msg["To"]      = to_email
            msg.attach(MIMEText(html_body, "html"))

            with smtplib.SMTP(SMTP_CONFIG["host"], SMTP_CONFIG["port"]) as server:
                server.ehlo()
                server.starttls()
                server.login(SMTP_CONFIG["username"], SMTP_CONFIG["password"])
                server.sendmail(SMTP_CONFIG["username"], to_email, msg.as_string())
            print(f"[EMAIL] Sent '{subject}' -> {to_email}")
            return True
        except Exception as exc:
            print(f"[EMAIL ERROR] {exc}")
            return False

    # ── WELCOME MAIL ─────────────────────────────────────────
    @staticmethod
    def send_welcome(user_name: str, to_email: str) -> bool:
        subject = "Welcome to MediHabit 💊"
        html = f"""
        <html><body style="font-family:Arial,sans-serif;background:#f4f8fb;padding:20px;">
          <div style="max-width:560px;margin:auto;background:#fff;
                      border-radius:12px;padding:32px;box-shadow:0 2px 8px rgba(0,0,0,.1);">
            <h1 style="color:#2c7be5;">Welcome to MediHabit!</h1>
            <p style="font-size:16px;">Hi <strong>{user_name}</strong>,</p>
            <p>We're delighted to have you on board. MediHabit helps you:</p>
            <ul>
              <li>📅 Track your daily medicines</li>
              <li>🔔 Receive timely email &amp; voice reminders</li>
              <li>📊 Monitor your adherence history</li>
              <li>🏥 Manage refill alerts before stock runs out</li>
            </ul>
            <p>Start by adding your first medicine in the app.</p>
            <a href="#" style="display:inline-block;margin-top:12px;padding:12px 24px;
               background:#2c7be5;color:#fff;border-radius:8px;text-decoration:none;
               font-weight:bold;">Get Started</a>
            <hr style="margin:24px 0;border:none;border-top:1px solid #eee;">
            <p style="color:#888;font-size:12px;">
              If you did not create this account, please ignore this email.
            </p>
          </div>
        </body></html>"""
        return EmailService._send(to_email, subject, html)

    # ── REMINDER MAIL ────────────────────────────────────────
    @staticmethod
    def send_reminder(user_name: str, to_email: str,
                      medicine_name: str, dosage: str,
                      scheduled_time: str) -> bool:
        subject = f"⏰ Medicine Reminder: {medicine_name}"
        html = f"""
        <html><body style="font-family:Arial,sans-serif;background:#f4f8fb;padding:20px;">
          <div style="max-width:560px;margin:auto;background:#fff;
                      border-radius:12px;padding:32px;box-shadow:0 2px 8px rgba(0,0,0,.1);">
            <h2 style="color:#e85d04;">⏰ Time to take your medicine!</h2>
            <p>Hi <strong>{user_name}</strong>,</p>
            <table style="width:100%;border-collapse:collapse;margin-top:16px;">
              <tr style="background:#f0f6ff;">
                <td style="padding:10px;font-weight:bold;">Medicine</td>
                <td style="padding:10px;">{medicine_name}</td>
              </tr>
              <tr>
                <td style="padding:10px;font-weight:bold;">Dosage</td>
                <td style="padding:10px;">{dosage}</td>
              </tr>
              <tr style="background:#f0f6ff;">
                <td style="padding:10px;font-weight:bold;">Scheduled Time</td>
                <td style="padding:10px;">{scheduled_time}</td>
              </tr>
            </table>
            <p style="margin-top:20px;">Please take your medicine as prescribed. 
               Stay consistent for best results!</p>
            <p style="color:#888;font-size:12px;margin-top:24px;">
              — MediHabit Reminder System
            </p>
          </div>
        </body></html>"""
        return EmailService._send(to_email, subject, html)

    # ── LOW STOCK ALERT ──────────────────────────────────────
    @staticmethod
    def send_low_stock_alert(user_name: str, to_email: str,
                             medicine_name: str, remaining: int) -> bool:
        subject = f"⚠️ Low Stock Alert: {medicine_name}"
        html = f"""
        <html><body style="font-family:Arial,sans-serif;background:#fff8f0;padding:20px;">
          <div style="max-width:560px;margin:auto;background:#fff;
                      border-radius:12px;padding:32px;box-shadow:0 2px 8px rgba(0,0,0,.1);">
            <h2 style="color:#d62828;">⚠️ Low Medicine Stock</h2>
            <p>Hi <strong>{user_name}</strong>,</p>
            <p>Your medicine <strong>{medicine_name}</strong> has only 
               <strong>{remaining} dose(s)</strong> remaining.</p>
            <p>Please refill your prescription soon to avoid missing doses.</p>
          </div>
        </body></html>"""
        return EmailService._send(to_email, subject, html)

    # ── PASSWORD RESET MAIL ──────────────────────────────────
    @staticmethod
    def send_password_reset(user_name: str, to_email: str, token: str) -> bool:
        reset_link = f"https://yourdomain.com/reset-password?token={token}"
        subject = "🔑 Password Reset – MediHabit"
        html = f"""
        <html><body style="font-family:Arial,sans-serif;padding:20px;">
          <div style="max-width:560px;margin:auto;background:#fff;
                      border-radius:12px;padding:32px;box-shadow:0 2px 8px rgba(0,0,0,.1);">
            <h2>Reset Your Password</h2>
            <p>Hi <strong>{user_name}</strong>,</p>
            <p>Click the button below to reset your password. 
               This link expires in <strong>30 minutes</strong>.</p>
            <a href="{reset_link}" style="display:inline-block;margin-top:12px;
               padding:12px 24px;background:#2c7be5;color:#fff;border-radius:8px;
               text-decoration:none;font-weight:bold;">Reset Password</a>
            <p style="margin-top:20px;color:#888;font-size:12px;">
              If you did not request this, ignore this email.
            </p>
          </div>
        </body></html>"""
        return EmailService._send(to_email, subject, html)


# ─────────────────────────────────────────────────────────────
# 4.  VOICE REMINDER SERVICE  (pyttsx3 – offline TTS)
# ─────────────────────────────────────────────────────────────
class VoiceReminderService:
    """
    Offline text-to-speech using pyttsx3.
    Install: pip install pyttsx3
    On Linux also: sudo apt install espeak
    """

    @staticmethod
    def speak(text: str) -> bool:
        if not TTS_AVAILABLE:
            print("[VOICE] pyttsx3 not installed. Install with: pip install pyttsx3")
            return False
        try:
            engine = pyttsx3.init()
            engine.setProperty("rate", 150)   # words per minute
            engine.setProperty("volume", 1.0)
            engine.say(text)
            engine.runAndWait()
            return True
        except Exception as exc:
            print(f"[VOICE ERROR] {exc}")
            return False

    @staticmethod
    def remind(user_name: str, medicine_name: str, dosage: str) -> bool:
        msg = (f"Hello {user_name}. This is your MediHabit reminder. "
               f"It is time to take {medicine_name}, {dosage}. "
               f"Please take your medicine now.")
        print(f"[VOICE] Speaking: {msg}")
        # Run in a daemon thread so it doesn't block the main process
        t = threading.Thread(target=VoiceReminderService.speak, args=(msg,), daemon=True)
        t.start()
        return True


# ─────────────────────────────────────────────────────────────
# 5.  USER SERVICE
# ─────────────────────────────────────────────────────────────
class UserService:

    # ── Register ─────────────────────────────────────────────
    @staticmethod
    def register(name: str, email: str, password: str,
                 phone: str = "", timezone: str = "Asia/Kolkata") -> dict:
        if not validate_email(email):
            return {"success": False, "error": "Invalid email address."}
        ok, msg = validate_password(password)
        if not ok:
            return {"success": False, "error": msg}

        salt = generate_salt()
        pw_hash = hash_password(password, salt)

        with get_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                return {"success": False, "error": "Email already registered."}

            conn.execute(
                """INSERT INTO users (name, email, phone, password_hash, salt, timezone)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, email, phone, pw_hash, salt, timezone))
            user_id = conn.execute(
                "SELECT last_insert_rowid()").fetchone()[0]

        # Send welcome email (non-blocking)
        threading.Thread(
            target=EmailService.send_welcome,
            args=(name, email), daemon=True).start()

        return {"success": True, "user_id": user_id,
                "message": f"User registered. Welcome email sent to {email}."}

    # ── Login ─────────────────────────────────────────────────
    @staticmethod
    def login(email: str, password: str) -> dict:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ? AND is_active = 1",
                (email,)).fetchone()
        if not row:
            return {"success": False, "error": "Invalid email or password."}

        pw_hash = hash_password(password, row["salt"])
        if pw_hash != row["password_hash"]:
            return {"success": False, "error": "Invalid email or password."}

        return {"success": True, "user": {
            "id": row["id"], "name": row["name"],
            "email": row["email"], "phone": row["phone"],
            "timezone": row["timezone"]}}

    # ── Get Profile ───────────────────────────────────────────
    @staticmethod
    def get_profile(user_id: int) -> dict:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, name, email, phone, timezone, created_at "
                "FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return {"success": False, "error": "User not found."}
        return {"success": True, "user": dict(row)}

    # ── Edit Profile ──────────────────────────────────────────
    @staticmethod
    def edit_profile(user_id: int, name: str = None,
                     phone: str = None, timezone: str = None) -> dict:
        fields, vals = [], []
        if name:
            fields.append("name = ?"); vals.append(name)
        if phone is not None:
            fields.append("phone = ?"); vals.append(phone)
        if timezone:
            fields.append("timezone = ?"); vals.append(timezone)
        if not fields:
            return {"success": False, "error": "No fields to update."}
        fields.append("updated_at = ?"); vals.append(now_str())
        vals.append(user_id)
        sql = f"UPDATE users SET {', '.join(fields)} WHERE id = ?"
        with get_connection() as conn:
            conn.execute(sql, vals)
        return {"success": True, "message": "Profile updated successfully."}

    # ── Change Password ───────────────────────────────────────
    @staticmethod
    def change_password(user_id: int, old_password: str,
                        new_password: str) -> dict:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return {"success": False, "error": "User not found."}
        if hash_password(old_password, row["salt"]) != row["password_hash"]:
            return {"success": False, "error": "Current password is incorrect."}

        ok, msg = validate_password(new_password)
        if not ok:
            return {"success": False, "error": msg}

        salt = generate_salt()
        pw_hash = hash_password(new_password, salt)
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET password_hash=?, salt=?, updated_at=? WHERE id=?",
                (pw_hash, salt, now_str(), user_id))
        return {"success": True, "message": "Password changed successfully."}

    # ── Forgot / Reset Password ───────────────────────────────
    @staticmethod
    def request_password_reset(email: str) -> dict:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, name FROM users WHERE email=? AND is_active=1",
                (email,)).fetchone()
        if not row:
            return {"success": False, "error": "Email not found."}

        token = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(minutes=30)).strftime(
            "%Y-%m-%d %H:%M:%S")
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO reset_tokens (user_id, token, expires_at) VALUES (?,?,?)",
                (row["id"], token, expires))

        threading.Thread(
            target=EmailService.send_password_reset,
            args=(row["name"], email, token), daemon=True).start()
        return {"success": True,
                "message": "Password reset email sent.", "token": token}

    @staticmethod
    def reset_password(token: str, new_password: str) -> dict:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM reset_tokens WHERE token=? AND used=0",
                (token,)).fetchone()
        if not row:
            return {"success": False, "error": "Invalid or expired token."}
        if datetime.now() > datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S"):
            return {"success": False, "error": "Token has expired."}

        ok, msg = validate_password(new_password)
        if not ok:
            return {"success": False, "error": msg}

        salt = generate_salt()
        pw_hash = hash_password(new_password, salt)
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET password_hash=?, salt=?, updated_at=? WHERE id=?",
                (pw_hash, salt, now_str(), row["user_id"]))
            conn.execute(
                "UPDATE reset_tokens SET used=1 WHERE token=?", (token,))
        return {"success": True, "message": "Password reset successful."}

    # ── Deactivate Account ────────────────────────────────────
    @staticmethod
    def deactivate_account(user_id: int) -> dict:
        with get_connection() as conn:
            conn.execute(
                "UPDATE users SET is_active=0, updated_at=? WHERE id=?",
                (now_str(), user_id))
        return {"success": True, "message": "Account deactivated."}


# ─────────────────────────────────────────────────────────────
# 6.  MEDICINE SERVICE
# ─────────────────────────────────────────────────────────────
class MedicineService:

    # ── Add Medicine ──────────────────────────────────────────
    @staticmethod
    def add_medicine(user_id: int, name: str, dosage: str,
                     times_of_day: list, start_date: str,
                     form: str = "Tablet", frequency: str = "Once daily",
                     end_date: str = None, stock_count: int = 0,
                     low_stock_alert: int = 5, notes: str = "") -> dict:
        if not name or not dosage:
            return {"success": False, "error": "Name and dosage are required."}
        if not times_of_day:
            return {"success": False, "error": "At least one reminder time required."}
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
        except ValueError:
            return {"success": False,
                    "error": "Invalid start_date format. Use YYYY-MM-DD."}

        times_json = json.dumps(times_of_day)
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO medicines
                   (user_id, name, dosage, form, frequency, times_of_day,
                    start_date, end_date, stock_count, low_stock_alert, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, name, dosage, form, frequency, times_json,
                 start_date, end_date, stock_count, low_stock_alert, notes))
            med_id = conn.execute(
                "SELECT last_insert_rowid()").fetchone()[0]
        return {"success": True, "medicine_id": med_id,
                "message": f"Medicine '{name}' added successfully."}

    # ── Get All Medicines for a User ──────────────────────────
    @staticmethod
    def get_medicines(user_id: int, active_only: bool = True) -> dict:
        sql = "SELECT * FROM medicines WHERE user_id = ?"
        if active_only:
            sql += " AND is_active = 1"
        sql += " ORDER BY name"
        with get_connection() as conn:
            rows = conn.execute(sql, (user_id,)).fetchall()
        meds = []
        for r in rows:
            m = dict(r)
            m["times_of_day"] = json.loads(m["times_of_day"])
            meds.append(m)
        return {"success": True, "medicines": meds, "count": len(meds)}

    # ── Get Single Medicine ───────────────────────────────────
    @staticmethod
    def get_medicine(medicine_id: int, user_id: int) -> dict:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM medicines WHERE id=? AND user_id=?",
                (medicine_id, user_id)).fetchone()
        if not row:
            return {"success": False, "error": "Medicine not found."}
        m = dict(row)
        m["times_of_day"] = json.loads(m["times_of_day"])
        return {"success": True, "medicine": m}

    # ── Edit Medicine ─────────────────────────────────────────
    @staticmethod
    def edit_medicine(medicine_id: int, user_id: int, **kwargs) -> dict:
        allowed = {"name", "dosage", "form", "frequency", "times_of_day",
                   "start_date", "end_date", "stock_count",
                   "low_stock_alert", "notes"}
        fields, vals = [], []
        for key, value in kwargs.items():
            if key not in allowed:
                continue
            if key == "times_of_day":
                value = json.dumps(value)
            fields.append(f"{key} = ?")
            vals.append(value)
        if not fields:
            return {"success": False, "error": "No valid fields to update."}
        fields.append("updated_at = ?"); vals.append(now_str())
        vals.extend([medicine_id, user_id])
        sql = f"UPDATE medicines SET {', '.join(fields)} WHERE id=? AND user_id=?"
        with get_connection() as conn:
            conn.execute(sql, vals)
        return {"success": True, "message": "Medicine updated."}

    # ── Delete / Deactivate Medicine ──────────────────────────
    @staticmethod
    def delete_medicine(medicine_id: int, user_id: int,
                        hard_delete: bool = False) -> dict:
        with get_connection() as conn:
            if hard_delete:
                conn.execute(
                    "DELETE FROM medicines WHERE id=? AND user_id=?",
                    (medicine_id, user_id))
            else:
                conn.execute(
                    "UPDATE medicines SET is_active=0, updated_at=? "
                    "WHERE id=? AND user_id=?",
                    (now_str(), medicine_id, user_id))
        return {"success": True, "message": "Medicine removed."}

    # ── Update Stock ──────────────────────────────────────────
    @staticmethod
    def update_stock(medicine_id: int, user_id: int,
                     quantity_delta: int) -> dict:
        """
        quantity_delta > 0 → refill (add tablets)
        quantity_delta < 0 → consume (mark as taken)
        """
        with get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM medicines WHERE id=? AND user_id=?",
                (medicine_id, user_id)).fetchone()
            if not row:
                return {"success": False, "error": "Medicine not found."}
            new_stock = max(0, row["stock_count"] + quantity_delta)
            conn.execute(
                "UPDATE medicines SET stock_count=?, updated_at=? WHERE id=?",
                (new_stock, now_str(), medicine_id))

        # Low-stock alert
        if new_stock <= row["low_stock_alert"] and new_stock >= 0:
            with get_connection() as conn:
                user = conn.execute(
                    "SELECT name, email FROM users WHERE id=?",
                    (user_id,)).fetchone()
            if user:
                threading.Thread(
                    target=EmailService.send_low_stock_alert,
                    args=(user["name"], user["email"],
                          row["name"], new_stock), daemon=True).start()

        return {"success": True, "stock_count": new_stock}


# ─────────────────────────────────────────────────────────────
# 7.  REMINDER SERVICE
# ─────────────────────────────────────────────────────────────
class ReminderService:
    """
    Fire email + voice reminders for a given medicine.
    Logs each reminder in reminder_logs table.
    """

    @staticmethod
    def fire_reminder(medicine_id: int) -> dict:
        with get_connection() as conn:
            med = conn.execute(
                "SELECT * FROM medicines WHERE id=? AND is_active=1",
                (medicine_id,)).fetchone()
            if not med:
                return {"success": False, "error": "Medicine not found."}
            user = conn.execute(
                "SELECT * FROM users WHERE id=?",
                (med["user_id"],)).fetchone()

        scheduled_at = now_str()
        times = json.loads(med["times_of_day"])
        current_time = datetime.now().strftime("%H:%M")

        # ── Email reminder ────────────────────────────────────
        email_ok = EmailService.send_reminder(
            user["name"], user["email"],
            med["name"], med["dosage"], current_time)

        # ── Voice reminder ────────────────────────────────────
        voice_ok = VoiceReminderService.remind(
            user["name"], med["name"], med["dosage"])

        # ── Log ───────────────────────────────────────────────
        status = "sent" if email_ok else "failed"
        with get_connection() as conn:
            conn.execute(
                """INSERT INTO reminder_logs
                   (user_id, medicine_id, scheduled_at, channel, status, sent_at)
                   VALUES (?, ?, ?, 'email', ?, ?)""",
                (user["id"], medicine_id, scheduled_at, status, now_str()))
            if voice_ok:
                conn.execute(
                    """INSERT INTO reminder_logs
                       (user_id, medicine_id, scheduled_at, channel, status, sent_at)
                       VALUES (?, ?, ?, 'voice', 'sent', ?)""",
                    (user["id"], medicine_id, scheduled_at, now_str()))

        return {"success": True, "email_sent": email_ok, "voice_played": voice_ok}

    @staticmethod
    def get_reminder_logs(user_id: int, limit: int = 50) -> dict:
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT rl.*, m.name AS medicine_name
                   FROM reminder_logs rl
                   JOIN medicines m ON m.id = rl.medicine_id
                   WHERE rl.user_id = ?
                   ORDER BY rl.created_at DESC LIMIT ?""",
                (user_id, limit)).fetchall()
        return {"success": True, "logs": [dict(r) for r in rows]}


# ─────────────────────────────────────────────────────────────
# 8.  ADHERENCE SERVICE
# ─────────────────────────────────────────────────────────────
class AdherenceService:

    @staticmethod
    def mark_dose(user_id: int, medicine_id: int,
                  scheduled_date: str, scheduled_time: str,
                  status: str, notes: str = "") -> dict:
        if status not in ("taken", "skipped"):
            return {"success": False, "error": "Status must be 'taken' or 'skipped'."}
        with get_connection() as conn:
            existing = conn.execute(
                """SELECT id FROM adherence
                   WHERE user_id=? AND medicine_id=?
                   AND scheduled_date=? AND scheduled_time=?""",
                (user_id, medicine_id, scheduled_date, scheduled_time)).fetchone()
            if existing:
                conn.execute(
                    "UPDATE adherence SET status=?, marked_at=?, notes=? WHERE id=?",
                    (status, now_str(), notes, existing["id"]))
            else:
                conn.execute(
                    """INSERT INTO adherence
                       (user_id, medicine_id, scheduled_date, scheduled_time,
                        status, marked_at, notes)
                       VALUES (?,?,?,?,?,?,?)""",
                    (user_id, medicine_id, scheduled_date,
                     scheduled_time, status, now_str(), notes))

        # Decrease stock when taken
        if status == "taken":
            MedicineService.update_stock(medicine_id, user_id, -1)

        return {"success": True, "message": f"Dose marked as {status}."}

    @staticmethod
    def get_adherence_report(user_id: int,
                             from_date: str, to_date: str) -> dict:
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT a.*, m.name AS medicine_name
                   FROM adherence a
                   JOIN medicines m ON m.id = a.medicine_id
                   WHERE a.user_id = ?
                   AND a.scheduled_date BETWEEN ? AND ?
                   ORDER BY a.scheduled_date, a.scheduled_time""",
                (user_id, from_date, to_date)).fetchall()

        records = [dict(r) for r in rows]
        total   = len(records)
        taken   = sum(1 for r in records if r["status"] == "taken")
        skipped = sum(1 for r in records if r["status"] == "skipped")
        missed  = sum(1 for r in records if r["status"] == "missed")
        rate    = round((taken / total * 100), 1) if total else 0

        return {
            "success": True,
            "from_date": from_date, "to_date": to_date,
            "total": total, "taken": taken,
            "skipped": skipped, "missed": missed,
            "adherence_rate_percent": rate,
            "records": records}

    @staticmethod
    def mark_missed_doses() -> None:
        """Called by scheduler daily at midnight to mark un-acknowledged doses."""
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        with get_connection() as conn:
            meds = conn.execute(
                "SELECT * FROM medicines WHERE is_active=1").fetchall()
            for med in meds:
                times = json.loads(med["times_of_day"])
                for t in times:
                    existing = conn.execute(
                        """SELECT id FROM adherence
                           WHERE medicine_id=? AND scheduled_date=?
                           AND scheduled_time=?""",
                        (med["id"], yesterday, t)).fetchone()
                    if not existing:
                        conn.execute(
                            """INSERT INTO adherence
                               (user_id, medicine_id, scheduled_date,
                                scheduled_time, status)
                               VALUES (?,?,?,?,'missed')""",
                            (med["user_id"], med["id"], yesterday, t))
        print(f"[ADHERENCE] Missed doses marked for {yesterday}.")


# ─────────────────────────────────────────────────────────────
# 9.  SCHEDULER  (APScheduler – background jobs)
# ─────────────────────────────────────────────────────────────
class SchedulerService:
    """
    Scans active medicines every minute and fires reminders
    whose scheduled time matches the current HH:MM.
    """

    def __init__(self):
        if not SCHEDULER_AVAILABLE:
            print("[SCHEDULER] APScheduler not installed. "
                  "Install: pip install apscheduler")
            return
        self.scheduler = BackgroundScheduler()

    def _check_reminders(self):
        current_time = datetime.now().strftime("%H:%M")
        today = today_str()
        with get_connection() as conn:
            meds = conn.execute(
                """SELECT * FROM medicines
                   WHERE is_active=1
                   AND (end_date IS NULL OR end_date >= ?)""",
                (today,)).fetchall()

        for med in meds:
            times = json.loads(med["times_of_day"])
            if current_time in times:
                print(f"[SCHEDULER] Firing reminder for medicine_id={med['id']} "
                      f"at {current_time}")
                ReminderService.fire_reminder(med["id"])

    def _mark_missed(self):
        AdherenceService.mark_missed_doses()

    def start(self):
        if not SCHEDULER_AVAILABLE:
            return
        # Check reminders every minute
        self.scheduler.add_job(
            self._check_reminders, "interval", minutes=1,
            id="reminder_check")
        # Mark missed doses at 00:05 daily
        self.scheduler.add_job(
            self._mark_missed,
            CronTrigger(hour=0, minute=5),
            id="mark_missed")
        self.scheduler.start()
        print("[SCHEDULER] Background scheduler started.")

    def stop(self):
        if not SCHEDULER_AVAILABLE:
            return
        self.scheduler.shutdown()
        print("[SCHEDULER] Scheduler stopped.")


# ─────────────────────────────────────────────────────────────
# 10. DASHBOARD / ANALYTICS
# ─────────────────────────────────────────────────────────────
class DashboardService:

    @staticmethod
    def get_today_schedule(user_id: int) -> dict:
        today = today_str()
        with get_connection() as conn:
            meds = conn.execute(
                """SELECT * FROM medicines
                   WHERE user_id=? AND is_active=1
                   AND start_date <= ?
                   AND (end_date IS NULL OR end_date >= ?)""",
                (user_id, today, today)).fetchall()

        schedule = []
        for med in meds:
            times = json.loads(med["times_of_day"])
            with get_connection() as conn:
                for t in times:
                    adherence = conn.execute(
                        """SELECT status FROM adherence
                           WHERE medicine_id=? AND scheduled_date=?
                           AND scheduled_time=?""",
                        (med["id"], today, t)).fetchone()
                    status = adherence["status"] if adherence else "pending"
                    schedule.append({
                        "medicine_id":   med["id"],
                        "medicine_name": med["name"],
                        "dosage":        med["dosage"],
                        "form":          med["form"],
                        "time":          t,
                        "status":        status,
                        "stock":         med["stock_count"],
                    })
        schedule.sort(key=lambda x: x["time"])
        return {"success": True, "date": today,
                "schedule": schedule, "count": len(schedule)}

    @staticmethod
    def get_summary(user_id: int) -> dict:
        with get_connection() as conn:
            total_meds = conn.execute(
                "SELECT COUNT(*) FROM medicines WHERE user_id=? AND is_active=1",
                (user_id,)).fetchone()[0]
            today_taken = conn.execute(
                """SELECT COUNT(*) FROM adherence
                   WHERE user_id=? AND scheduled_date=? AND status='taken'""",
                (user_id, today_str())).fetchone()[0]
            today_missed = conn.execute(
                """SELECT COUNT(*) FROM adherence
                   WHERE user_id=? AND scheduled_date=? AND status='missed'""",
                (user_id, today_str())).fetchone()[0]
            low_stock = conn.execute(
                """SELECT name, stock_count FROM medicines
                   WHERE user_id=? AND is_active=1
                   AND stock_count <= low_stock_alert""",
                (user_id,)).fetchall()

        return {
            "success": True,
            "total_active_medicines": total_meds,
            "today_taken": today_taken,
            "today_missed": today_missed,
            "low_stock_medicines": [dict(r) for r in low_stock]}


# ─────────────────────────────────────────────────────────────
# 11. MAIN  –  Demo / Quick Test
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  MediHabit Backend – Initialising")
    print("=" * 60)

    # 1. Init database
    init_db()

    # 2. Register a test user
    res = UserService.register(
        name="Chaitanya",
        email="chaitanya@example.com",
        password="SecurePass1",
        phone="+91-9876543210",
        timezone="Asia/Kolkata")
    print("\n[REGISTER]", res)

    user_id = res.get("user_id", 1)

    # 3. Login
    login_res = UserService.login("chaitanya@example.com", "SecurePass1")
    print("\n[LOGIN]", login_res)

    # 4. Edit profile
    edit_res = UserService.edit_profile(
        user_id, name="Chaitanya Kumar", phone="+91-9999999999")
    print("\n[EDIT PROFILE]", edit_res)

    # 5. Add medicines
    m1 = MedicineService.add_medicine(
        user_id=user_id,
        name="Metformin",
        dosage="500 mg",
        form="Tablet",
        frequency="Twice daily",
        times_of_day=["08:00", "20:00"],
        start_date=today_str(),
        stock_count=60,
        low_stock_alert=10,
        notes="Take with food")
    print("\n[ADD MEDICINE 1]", m1)

    m2 = MedicineService.add_medicine(
        user_id=user_id,
        name="Vitamin D3",
        dosage="60000 IU",
        form="Capsule",
        frequency="Once weekly",
        times_of_day=["09:00"],
        start_date=today_str(),
        stock_count=8,
        low_stock_alert=2)
    print("\n[ADD MEDICINE 2]", m2)

    # 6. Get all medicines
    meds = MedicineService.get_medicines(user_id)
    print(f"\n[MEDICINES] Found {meds['count']} medicine(s).")

    # 7. Today's schedule
    schedule = DashboardService.get_today_schedule(user_id)
    print(f"\n[TODAY SCHEDULE] {schedule['count']} dose(s) scheduled.")

    # 8. Mark a dose as taken
    if m1["success"]:
        mark = AdherenceService.mark_dose(
            user_id=user_id,
            medicine_id=m1["medicine_id"],
            scheduled_date=today_str(),
            scheduled_time="08:00",
            status="taken")
        print("\n[MARK DOSE]", mark)

    # 9. Adherence report
    report = AdherenceService.get_adherence_report(
        user_id, today_str(), today_str())
    print(f"\n[ADHERENCE REPORT] Rate: {report['adherence_rate_percent']}%")

    # 10. Dashboard summary
    summary = DashboardService.get_summary(user_id)
    print("\n[SUMMARY]", summary)

    # 11. Fire a manual reminder (email + voice)
    if m1["success"]:
        print("\n[REMINDER] Firing manual reminder...")
        r = ReminderService.fire_reminder(m1["medicine_id"])
        print("[REMINDER RESULT]", r)

    # 12. Start background scheduler
    scheduler = SchedulerService()
    scheduler.start()
    print("\n[INFO] Backend running. Press Ctrl+C to stop.")
    try:
        import time
        time.sleep(5)   # demo: keep alive briefly
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
        print("[INFO] MediHabit backend stopped.")
"""
=============================================================
  INSTALLATION
=============================================================
  pip install apscheduler pyttsx3 reportlab

  On Linux for voice:
    sudo apt install espeak libespeak1

  SMTP: Use Gmail with 2-Step Verification enabled.
  Create an App Password at myaccount.google.com/apppasswords
  and replace SMTP_CONFIG['password'] with it.

=============================================================
  PROJECT STRUCTURE
=============================================================
  medihabit_backend.py   <-- this file (single-module backend)
  medihabit.db           <-- auto-created SQLite database

=============================================================
  API SURFACE (call these from Flask/FastAPI/CLI)
=============================================================
  UserService.register(...)
  UserService.login(...)
  UserService.get_profile(user_id)
  UserService.edit_profile(user_id, ...)
  UserService.change_password(user_id, old, new)
  UserService.request_password_reset(email)
  UserService.reset_password(token, new_password)
  UserService.deactivate_account(user_id)

  MedicineService.add_medicine(user_id, ...)
  MedicineService.get_medicines(user_id)
  MedicineService.get_medicine(medicine_id, user_id)
  MedicineService.edit_medicine(medicine_id, user_id, **kwargs)
  MedicineService.delete_medicine(medicine_id, user_id)
  MedicineService.update_stock(medicine_id, user_id, delta)

  ReminderService.fire_reminder(medicine_id)
  ReminderService.get_reminder_logs(user_id)

  AdherenceService.mark_dose(user_id, medicine_id, date, time, status)
  AdherenceService.get_adherence_report(user_id, from_date, to_date)

  DashboardService.get_today_schedule(user_id)
  DashboardService.get_summary(user_id)

  SchedulerService().start()   # starts background job engine
=============================================================
"""

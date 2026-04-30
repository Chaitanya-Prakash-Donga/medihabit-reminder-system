"""
MediHabit - app.py
Full Flask backend: auth, CRUD, Gmail SMTP (SSL 465)
Trigger-based reminders for universal timezone support.
"""
import os
import threading
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash, send_from_directory, jsonify)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECURITY_KEY', os.environ.get('SECRET_KEY', 'medihabit-super-secret-123'))

def get_now_naive():
    return datetime.now().replace(tzinfo=None)

uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)


# ── Gmail REST API Email (HTTPS only — works on Render free tier) ─────────────
# 
# SETUP INSTRUCTIONS (one-time):
# 1. Go to https://console.cloud.google.com/
# 2. Create a project → Enable "Gmail API"
# 3. OAuth consent screen → External → Add your Gmail as test user
# 4. Credentials → Create OAuth 2.0 Client ID → Desktop App
# 5. Download the client_secret JSON, note client_id and client_secret
# 6. Run this once locally to get your refresh_token:
#
#    from google_auth_oauthlib.flow import InstalledAppFlow
#    flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', ['https://www.googleapis.com/auth/gmail.send'])
#    creds = flow.run_local_server(port=0)
#    print("REFRESH TOKEN:", creds.refresh_token)
#
# 7. Set these 3 environment variables in Render:
#    GMAIL_CLIENT_ID      → from Google Cloud Console
#    GMAIL_CLIENT_SECRET  → from Google Cloud Console
#    GMAIL_REFRESH_TOKEN  → from step 6
#    GMAIL_USER           → your Gmail address (e.g. you@gmail.com)
#
# ─────────────────────────────────────────────────────────────────────────────

def _get_gmail_access_token():
    """Exchange the stored refresh token for a short-lived access token."""
    client_id     = os.environ.get('GMAIL_CLIENT_ID')
    client_secret = os.environ.get('GMAIL_CLIENT_SECRET')
    refresh_token = os.environ.get('GMAIL_REFRESH_TOKEN')

    if not all([client_id, client_secret, refresh_token]):
        print("❌ Missing GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN env vars")
        return None

    data = urllib.parse.urlencode({
        'client_id':     client_id,
        'client_secret': client_secret,
        'refresh_token': refresh_token,
        'grant_type':    'refresh_token',
    }).encode()

    req = urllib.request.Request(
        'https://oauth2.googleapis.com/token',
        data=data,
        method='POST'
    )
    req.add_header('Content-Type', 'application/x-www-form-urlencoded')

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return result.get('access_token')
    except Exception as e:
        print(f"❌ Token refresh error: {e}")
        return None


def send_smtp_email(to_email, subject, body):
    """
    Sends email via Gmail REST API (HTTPS port 443).
    Drop-in replacement for the old SMTP function — same signature.
    """
    import base64
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    sender_email = os.environ.get('GMAIL_USER')
    if not sender_email:
        print("❌ GMAIL_USER env var not set")
        return False

    access_token = _get_gmail_access_token()
    if not access_token:
        return False

    # Build the MIME message
    msg = MIMEMultipart()
    msg['From']    = f"MediHabit <{sender_email}>"
    msg['To']      = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

    # Gmail API expects base64url-encoded raw message
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = json.dumps({'raw': raw}).encode()

    req = urllib.request.Request(
        'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
        data=payload,
        method='POST'
    )
    req.add_header('Authorization', f'Bearer {access_token}')
    req.add_header('Content-Type', 'application/json')

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f"✅ Email sent to {to_email} | Message ID: {result.get('id')}")
            return True
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(f"❌ Gmail API HTTPError {e.code}: {error_body}")
        return False
    except Exception as e:
        print(f"❌ Gmail API error: {e}")
        return False


# ── Models ────────────────────────────────────────────────────────────────────
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=get_now_naive)
    medications = db.relationship('Medication', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

class Medication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    dose = db.Column(db.String(100))
    frequency = db.Column(db.String(50))
    time1 = db.Column(db.String(5))
    time2 = db.Column(db.String(5), nullable=True)
    recipient_email = db.Column(db.String(120))
    notes = db.Column(db.String(300))
    active = db.Column(db.Boolean, default=True)
    email_enabled = db.Column(db.Boolean, default=True)

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medication_name = db.Column(db.String(200))
    recipient = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20))


# ── Helpers ───────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ── Automated Reminder Engine ─────────────────────────────────────────────────
def send_reminder_task(med_id, log_id=None):
    """Handles the actual email sending and database logging."""
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med or not med.active:
            return

        subject = f"💊 Time for {med.name}"
        body = (f"Hello,\n\nThis is a reminder to take your medication:\n"
                f"Medication: {med.name}\n"
                f"Dosage: {med.dose}\n"
                f"Notes: {med.notes if med.notes else 'N/A'}\n\n"
                f"Sent via MediHabit Reminder System.")

        success = send_smtp_email(med.recipient_email, subject, body)

        if log_id:
            log = AlertLog.query.get(log_id)
            if log:
                log.status = 'sent' if success else 'failed'
                db.session.commit()
        else:
            new_log = AlertLog(
                user_id=med.user_id,
                medication_name=med.name,
                status='sent' if success else 'failed',
                recipient=med.recipient_email,
                sent_at=get_now_naive()
            )
            db.session.add(new_log)
            db.session.commit()


def check_and_send():
    """Background job that runs every minute to check scheduled times."""
    with app.app_context():
        now_str = get_now_naive().strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for m in meds:
            if m.time1 == now_str or m.time2 == now_str:
                threading.Thread(target=send_reminder_task, args=(m.id,), daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return redirect(url_for('dashboard')) if 'user_id' in session else redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            name = request.form.get('name')
            email = request.form.get('email').strip().lower()
            pw = request.form.get('password')
            if User.query.filter_by(email=email).first():
                flash("Email already registered!", "danger")
                return redirect(url_for('register'))
            user = User(name=name, email=email)
            user.set_password(pw)
            db.session.add(user)
            db.session.commit()

            threading.Thread(target=send_smtp_email, args=(
                email,
                "Welcome to MediHabit!",
                f"Hello {name},\n\nThank you for joining MediHabit. Your account is ready!"
            )).start()

            flash("Account created! Please login.", "success")
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            flash(f"Error: {str(e)}", "danger")
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        pw = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(pw):
            session.update({'user_id': user.id, 'user_name': user.name, 'user_email': user.email})
            return redirect(url_for('dashboard'))
        flash("Invalid email or password.", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    uid = session.get('user_id')
    meds = Medication.query.filter_by(user_id=uid).all()
    meds_js = [{"id": m.id, "name": m.name, "t1": m.time1, "t2": m.time2} for m in meds]
    today_date = get_now_naive().date()
    logs = AlertLog.query.filter(
        AlertLog.user_id == uid,
        db.func.date(AlertLog.sent_at) == today_date
    ).order_by(AlertLog.sent_at.desc()).all()

    return render_template('dashboard.html',
                           meds=meds,
                           meds_js=meds_js,
                           logs=logs,
                           today_date=datetime.now().strftime('%A, %d %B'))

@app.route('/medication/add', methods=['POST'])
@login_required
def add_medication():
    m = Medication(
        user_id=session['user_id'],
        name=request.form.get('name'),
        dose=request.form.get('dose'),
        time1=request.form.get('time1'),
        time2=request.form.get('time2') or None,
        recipient_email=request.form.get('recipient_email'),
        notes=request.form.get('notes'),
        email_enabled=True
    )
    db.session.add(m)
    db.session.commit()
    flash(f'"{m.name}" scheduled!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/trigger-reminder/<int:med_id>', methods=['POST'])
@login_required
def trigger_reminder(med_id):
    med = Medication.query.get(med_id)
    if not med:
        return jsonify({"status": "not_found"}), 404

    already_sent = AlertLog.query.filter(
        AlertLog.user_id == session['user_id'],
        AlertLog.medication_name == med.name,
        AlertLog.sent_at >= datetime.now() - timedelta(minutes=2)
    ).first()

    if not already_sent:
        new_log = AlertLog(
            user_id=session['user_id'],
            medication_name=med.name,
            status='pending',
            recipient=med.recipient_email,
            sent_at=get_now_naive()
        )
        db.session.add(new_log)
        db.session.commit()

        threading.Thread(target=send_reminder_task, args=(med.id, new_log.id), daemon=True).start()
        return jsonify({"status": "received"}), 200

    return jsonify({"status": "duplicate_prevented"}), 200


# ── PWA & Service Worker ──────────────────────────────────────────────────────
@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js')


# ── Startup & Scheduler ───────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

scheduler = BackgroundScheduler()
if not scheduler.running:
    scheduler.add_job(check_and_send, 'interval', minutes=1,
                      id='med_reminder_job', replace_existing=True)
    scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)

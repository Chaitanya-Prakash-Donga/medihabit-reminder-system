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
import base64
from datetime import datetime, timedelta
from functools import wraps
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash, send_from_directory, jsonify)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# --- App & DB setup ---
app = Flask(__name__)
# Uses SECURITY_KEY from your Render Environment Variables
app.secret_key = os.environ.get('SECURITY_KEY', 'medihabit-fallback-secret-123')

def get_now_naive():
    return datetime.now().replace(tzinfo=None)

# Database URL fix for SQLAlchemy/Render
uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)

# --- Gmail REST API Integration ---
def _get_gmail_access_token():
    """Exchange OAuth2 refresh token for a short-lived access token."""
    client_id     = os.environ.get('GMAIL_CLIENT_ID')
    client_secret = os.environ.get('GMAIL_CLIENT_SECRET')
    refresh_token = os.environ.get('GMAIL_REFRESH_TOKEN')

    if not all([client_id, client_secret, refresh_token]):
        print("❌ Missing Gmail API environment variables")
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

def send_gmail_api(to_email, subject, body):
    """Sends email via Gmail REST API (HTTPS port 443)."""
    sender_email = os.environ.get('GMAIL_USER')
    access_token = _get_gmail_access_token()

    if not sender_email or not access_token:
        return False

    msg = MIMEMultipart()
    msg['From']    = f"MediHabit <{sender_email}>"
    msg['To']      = to_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))

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
            print(f"✅ Email sent to {to_email} | ID: {result.get('id')}")
            return True
    except Exception as e:
        print(f"❌ Gmail API error: {e}")
        return False

# --- Database Models ---
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
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
    time1 = db.Column(db.String(5)) # Format "HH:MM"
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
    sent_at = db.Column(db.DateTime, default=get_now_naive)
    status = db.Column(db.String(20))

# --- Reminder Engine ---
def send_reminder_task(med_id, log_id=None):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med or not med.active: return

        subject = f"💊 Time for {med.name}"
        body = f"Hello,\n\nReminder: Take your {med.name} ({med.dose}).\nNotes: {med.notes}\n\nSent via MediHabit."
        
        success = send_gmail_api(med.recipient_email, subject, body)

        if log_id:
            log = AlertLog.query.get(log_id)
            if log:
                log.status = 'sent' if success else 'failed'
                db.session.commit()
        else:
            new_log = AlertLog(user_id=med.user_id, medication_name=med.name, 
                               recipient=med.recipient_email, status='sent' if success else 'failed')
            db.session.add(new_log)
            db.session.commit()

def check_and_send():
    """Background job checking every minute."""
    with app.app_context():
        now_str = get_now_naive().strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for m in meds:
            if m.time1 == now_str or m.time2 == now_str:
                threading.Thread(target=send_reminder_task, args=(m.id,), daemon=True).start()

# --- Routes ---
@app.route('/')
def index():
    return redirect(url_for('dashboard')) if 'user_id' in session else redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(request.form.get('password')):
            session.update({'user_id': user.id, 'user_name': user.name})
            return redirect(url_for('dashboard'))
        flash("Invalid credentials", "danger")
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session: return redirect(url_for('login'))
    meds = Medication.query.filter_by(user_id=session['user_id']).all()
    return render_template('dashboard.html', meds=meds)

# --- Startup ---
with app.app_context():
    db.create_all()

scheduler = BackgroundScheduler()
scheduler.add_job(check_and_send, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)

"""
MediHabit - app.py
Final Version: Gmail SMTP Alerts, IST Timezone Enforcement,
and full CRUD for Medications and Profiles.
"""
import os
import threading
import smtplib
import pytz
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'medihabit-super-secret-key-123')

# ── 1. TIMEZONE CONFIGURATION ────────────────────────────────────────────────
# Enforces Indian Standard Time regardless of server location
IST = pytz.timezone('Asia/Kolkata')

def get_ist_time():
    """Helper to get the current timestamp in IST."""
    return datetime.now(IST)

# ── 2. DATABASE CONFIGURATION ────────────────────────────────────────────────
uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)

# ── 3. GMAIL SMTP LOGIC ──────────────────────────────────────────────────────
# Pulls credentials from your Render Environment Variables
GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_PASS = os.environ.get('GMAIL_PASSWORD') 

def send_smtp_email(to_email, subject, body):
    """Sends email using Gmail SMTP."""
    if not GMAIL_USER or not GMAIL_PASS:
        print("❌ Error: GMAIL_USER or GMAIL_PASSWORD not set in Render Environment.")
        return False
    
    try:
        msg = MIMEMultipart()
        msg['From'] = f"MediHabit <{GMAIL_USER}>"
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"❌ Gmail SMTP Error: {str(e)}") 
        return False

# ── 4. DATABASE MODELS ───────────────────────────────────────────────────────
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=get_ist_time)
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

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medication_name = db.Column(db.String(200))
    recipient = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime, default=get_ist_time)
    status = db.Column(db.String(20), default='sent')

# ── 5. ROUTES & AUTHENTICATION ───────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

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
            threading.Thread(target=send_smtp_email, args=(email, "Welcome! 💊", f"Hi {name}, welcome to MediHabit!")).start()
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
            session.update({'user_id': user.id, 'user_name': user.name})
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
    now_ist = get_ist_time()
    # Filters logs to show only today's alerts in IST
    logs = AlertLog.query.filter(
        AlertLog.user_id == uid, 
        db.func.date(AlertLog.sent_at) == now_ist.date()
    ).order_by(AlertLog.sent_at.desc()).all()
    return render_template('dashboard.html', meds=meds, logs=logs, today_date=now_ist.strftime('%A, %d %b %Y'))

@app.route('/medication/add', methods=['POST'])
@login_required
def add_medication():
    m = Medication(
        user_id=session['user_id'],
        name=request.form.get('name'),
        dose=request.form.get('dose'),
        frequency=request.form.get('frequency'),
        time1=request.form.get('time1'),
        time2=request.form.get('time2') or None,
        recipient_email=request.form.get('recipient_email'),
        notes=request.form.get('notes')
    )
    db.session.add(m)
    db.session.commit()
    flash("Medication scheduled!", "success")
    return redirect(url_for('dashboard'))

@app.route('/medication/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id != session['user_id']: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        med.name = request.form.get('name')
        med.dose = request.form.get('dose')
        med.time1 = request.form.get('time1')
        med.time2 = request.form.get('time2') or None
        med.recipient_email = request.form.get('recipient_email')
        db.session.commit()
        flash("Medication updated!", "success")
        return redirect(url_for('dashboard'))
    return render_template('edit_medication.html', med=med)

@app.route('/medication/delete/<int:id>')
@login_required
def delete_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id == session['user_id']:
        db.session.delete(med)
        db.session.commit()
        flash("Medication removed.", "success")
    return redirect(url_for('dashboard'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        user.name = request.form.get('name') or user.name
        new_pw = request.form.get('password')
        if new_pw: user.set_password(new_pw)
        db.session.commit()
        session['user_name'] = user.name
        flash("Profile updated!", "success")
        return redirect(url_for('dashboard'))
    return render_template('edit_profile.html', user=user)

# ── 6. REMINDER ENGINE ───────────────────────────────────────────────────────
def send_reminder_task(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med or not med.active: return
        subject = f"💊 Time for {med.name}"
        body = f"Reminder: It is time to take {med.name} ({med.dose}).\nNotes: {med.notes}"
        success = send_smtp_email(med.recipient_email, subject, body)
        
        log = AlertLog(
            user_id=med.user_id, medication_name=med.name, 
            status='sent' if success else 'failed', #
            recipient=med.recipient_email, sent_at=get_ist_time() 
        )
        db.session.add(log)
        db.session.commit()

def check_and_send():
    with app.app_context():
        now_str = get_ist_time().strftime('%H:%M')
        meds = Medication.query.filter_by(active=True).all()
        for m in meds:
            if m.time1 == now_str or m.time2 == now_str:
                threading.Thread(target=send_reminder_task, args=(m.id,), daemon=True).start()

# ── 7. STARTUP ───────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

# Scheduler set to IST for accurate triggers
scheduler = BackgroundScheduler(timezone=IST)
scheduler.add_job(check_and_send, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)

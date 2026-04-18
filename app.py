"""
MediHabit - app.py
Full Flask backend: auth, CRUD, Gmail SMTP email alerts, APScheduler
"""
import os
import threading
import smtplib
import pytz
from datetime import datetime
from email.mime.text import MIMEText
from functools import wraps

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'medihabit-super-secret-key-123')

# 1. FIXED TIMEZONE LOGIC (Force India Standard Time)
IST = pytz.timezone('Asia/Kolkata')

uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)

# 2. SMTP CONFIGURATION (Direct Gmail - No 3rd Party APIs) ─────────────────────
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
# Ensure these are set in Render Environment Variables
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')      
SENDER_PASSWORD = os.environ.get('SENDER_PASSWORD') 

def send_direct_email(to_email, subject, body):
    """Sends email using standard Python smtplib with error logging."""
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        print("❌ EMAIL ERROR: SENDER_EMAIL or SENDER_PASSWORD not set in Env Vars.")
        return False
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = f"MediHabit Reminder <{SENDER_EMAIL}>"
        msg['To'] = to_email

        # Set a timeout so the app doesn't hang if Gmail is slow
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=15) as server:
            server.starttls()  # Secure connection
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, to_email, msg.as_string())
        
        print(f"✅ Email successfully sent to {to_email} at {datetime.now(IST)}")
        return True
    except Exception as e:
        print(f"❌ SMTP Error: {str(e)}")
        return False

# ── Models ────────────────────────────────────────────────────────────────────
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(IST))
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
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(IST))
    status = db.Column(db.String(20))

# ── Helpers & Routes ──────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def index():
    return redirect(url_for('dashboard')) if 'user_id' in session else redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email').strip().lower()
        pw = request.form.get('password')
        if User.query.filter_by(email=email).first():
            flash("Email already exists!", "danger")
            return redirect(url_for('register'))
        user = User(name=name, email=email)
        user.set_password(pw)
        db.session.add(user)
        db.session.commit()
        flash("Registration successful!", "success")
        return redirect(url_for('login'))
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
    meds_js = [{"name": m.name, "t1": m.time1, "t2": m.time2} for m in meds]
    
    now_ist = datetime.now(IST)
    logs = AlertLog.query.filter(
        AlertLog.user_id == uid, 
        db.func.date(AlertLog.sent_at) == now_ist.date()
    ).order_by(AlertLog.sent_at.desc()).all()
    
    return render_template('dashboard.html', meds=meds, meds_js=meds_js, logs=logs, today_date=now_ist.strftime('%A, %d %b %Y'))

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
        notes=request.form.get('notes')
    )
    db.session.add(m)
    db.session.commit()
    flash("Medication Added!", "success")
    return redirect(url_for('dashboard'))

@app.route('/medication/delete/<int:id>')
@login_required
def delete_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id == session['user_id']:
        db.session.delete(med)
        db.session.commit()
    return redirect(url_for('dashboard'))

# ── THE REMINDER ENGINE ───────────────────────────────────────────────────────
def send_reminder_task(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med: return
        
        subject = f"💊 Time for your Medicine: {med.name}"
        body = f"Hello! It is time to take your {med.name} ({med.dose}).\n\nNotes: {med.notes}"
        
        success = send_direct_email(med.recipient_email, subject, body)
        
        log = AlertLog(
            user_id=med.user_id, 
            medication_name=med.name, 
            recipient=med.recipient_email, 
            status='sent' if success else 'failed',
            sent_at=datetime.now(IST)
        )
        db.session.add(log)
        db.session.commit()

def check_and_send():
    with app.app_context():
        # Get current time in India (HH:mm format)
        now_str = datetime.now(IST).strftime('%H:%M')
        print(f"⏰ Scheduler Checking at: {now_str} IST") # Debug log
        
        meds = Medication.query.filter_by(active=True).all()
        for m in meds:
            if m.time1 == now_str or m.time2 == now_str:
                # Use a thread so the scheduler doesn't get blocked
                threading.Thread(target=send_reminder_task, args=(m.id,), daemon=True).start()

# ── Startup ───────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

# Ensure scheduler uses IST for matching time1/time2
scheduler = BackgroundScheduler(timezone=IST)
scheduler.add_job(check_and_send, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)



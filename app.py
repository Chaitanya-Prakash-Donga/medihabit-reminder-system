import os
import threading
import smtplib
import pytz
from email.mime.text import MIMEText
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
# Secure key for sessions
app.secret_key = os.environ.get('SECRET_KEY', 'medihabit-super-secret-key-123')
IST = pytz.timezone('Asia/Kolkata')

# Handle database connection string for Render/Production
uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)

# ── SMTP Email Logic (In-House Logic) ────────────────────────────────────────
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
# These should be set in your Environment Variables
EMAIL_USER = os.environ.get('EMAIL_USER') 
# Use a Google 'App Password' if using Gmail
EMAIL_PASS = os.environ.get('EMAIL_PASS') 

def send_local_email(to_email, subject, body):
    """Sends email using standard Python smtplib without third-party APIs."""
    if not EMAIL_USER or not EMAIL_PASS:
        print("❌ Error: SMTP credentials not set in Environment Variables")
        return False

    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = f"MediHabit <{EMAIL_USER}>"
    msg['To'] = to_email

    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls() 
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print(f"✅ Email sent successfully to {to_email}")
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
    frequency = db.Column(db.String(50))
    time1 = db.Column(db.String(5))   
    time2 = db.Column(db.String(5), nullable=True)
    recipient_email = db.Column(db.String(120))
    notes = db.Column(db.String(300))
    email_enabled = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medication_name = db.Column(db.String(200))
    recipient = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(IST))
    status = db.Column(db.String(20), default='sent')

# ── Helpers ───────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

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
            
            # Send Welcome Email via standard SMTP
            welcome_body = f"Hi {name},\n\nWelcome to MediHabit! Your account is active. You can now start scheduling your medication reminders."
            threading.Thread(target=send_local_email, args=(email, "Welcome to MediHabit! 💊", welcome_body)).start()
            
            flash("Account created! A welcome email has been sent.", "success")
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
    today_display = datetime.now(IST).strftime('%A, %d %B %Y')
    logs = AlertLog.query.filter_by(user_id=uid).order_by(AlertLog.sent_at.desc()).limit(10).all()
    return render_template('dashboard.html', meds=meds, logs=logs, today_date=today_display)

@app.route('/medication/add', methods=['POST'])
@login_required
def add_medication():
    try:
        m = Medication(
            user_id=session['user_id'],
            name=request.form.get('name'),
            dose=request.form.get('dose'),
            frequency=request.form.get('frequency'),
            time1=request.form.get('time1'),
            time2=request.form.get('time2') or None,
            recipient_email=request.form.get('recipient_email'),
            notes=request.form.get('notes'),
            email_enabled='email_enabled' in request.form
        )
        db.session.add(m)
        db.session.commit()
        flash(f'"{m.name}" has been scheduled!', 'success')
    except Exception as e:
        db.session.rollback()
        flash("Error adding medication.", "danger")
    return redirect(url_for('dashboard'))

@app.route('/medication/delete/<int:id>')
@login_required
def delete_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id == session['user_id']:
        db.session.delete(med)
        db.session.commit()
        flash("Medication removed.", "success")
    return redirect(url_for('dashboard'))

# ── Reminder Engine ───────────────────────────────────────────────────────────
def send_reminder_task(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med or not med.email_enabled: return
        
        subject = f"💊 Time for {med.name}"
        body = f"Hello,\n\nThis is a reminder to take your medication: {med.name}\nDose: {med.dose}\nNotes: {med.notes}"
        success = send_local_email(med.recipient_email, subject, body)
        
        log = AlertLog(
            user_id=med.user_id, 
            medication_name=med.name, 
            status='sent' if success else 'failed', 
            recipient=med.recipient_email
        )
        db.session.add(log)
        db.session.commit()

def check_and_send():
    with app.app_context():
        now_str = datetime.now(IST).strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for m in meds:
            if m.time1 == now_str or m.time2 == now_str:
                threading.Thread(target=send_reminder_task, args=(m.id,), daemon=True).start()

# ── Startup ───────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

# Ensure scheduler only starts once
if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(check_and_send, 'interval', minutes=1)
    scheduler.start()

if __name__ == '__main__':
    app.run(debug=True)

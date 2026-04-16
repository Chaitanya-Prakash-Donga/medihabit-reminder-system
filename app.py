import os
import threading
import resend  # Use Resend instead of smtplib for Render compatibility
import pytz
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
# Secret key for session signing
app.secret_key = os.environ.get('SECRET_KEY', 'medihabit-super-secret-key-123')
IST = pytz.timezone('Asia/Kolkata')

# Database configuration (PostgreSQL for Render, SQLite for local)
uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)

# ── Resend API Email Logic ────────────────────────────────────────────────────
resend.api_key = os.environ.get('RESEND_API_KEY')

def send_smtp_email(to_email, subject, body):
    if not resend.api_key:
        print("❌ Error: RESEND_API_KEY not set in Environment Variables")
        return False

    try:
        params = {
            "from": "MediHabit <onboarding@resend.dev>",
            "to": [to_email],
            "subject": subject,
            "text": body,
        }
        resend.Emails.send(params)
        print(f"✅ Email sent successfully to {to_email}")
        return True
    except Exception as e:
        print(f"❌ Resend API Error: {str(e)}") 
        return False

# ── Models ────────────────────────────────────────────────────────────────────
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Kolkata')))
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
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Kolkata')))
    status = db.Column(db.String(20), default='sent')
    error = db.Column(db.String(300))

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
        flash("Invalid credentials.", "danger")
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    uid = session.get('user_id')
    meds = Medication.query.filter_by(user_id=uid).all() 
    tz = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(tz)
    
    start_of_day = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    logs = AlertLog.query.filter(
        AlertLog.user_id == uid, 
        AlertLog.sent_at >= start_of_day
    ).order_by(AlertLog.sent_at.desc()).all()
    
    return render_template('dashboard.html', meds=meds, logs=logs, today_date=now_ist.strftime('%A, %d %B %Y'))

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
        flash("Medication added!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Error adding medication: {str(e)}", "danger")
    return redirect(url_for('dashboard'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        try:
            # Check for name update
            new_name = request.form.get('name')
            if new_name:
                user.name = new_name
                session['user_name'] = new_name

            # Check for password update
            new_pw = request.form.get('password')
            if new_pw and len(new_pw.strip()) > 0:
                user.set_password(new_pw)

            db.session.commit()
            flash("Profile updated!", "success")
            return redirect(url_for('dashboard'))
        except Exception as e:
            db.session.rollback()
            flash("Update failed.", "danger")
    return render_template('edit_profile.html', user=user)

# ── Scheduler Logic ───────────────────────────────────────────────────────────
def check_and_send():
    with app.app_context():
        now_str = datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for m in meds:
            if m.time1 == now_str or m.time2 == now_str:
                # Logic to trigger email...
                pass

# ── Startup ───────────────────────────────────────────────────────────────────
with app.app_context():
    # db.drop_all() # ONLY UNCOMMENT THIS IF YOU NEED TO FIX SCHEMA ERRORS
    db.create_all()

scheduler = BackgroundScheduler(timezone=IST)
scheduler.add_job(check_and_send, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)

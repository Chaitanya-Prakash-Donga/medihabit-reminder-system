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

# Force IST Timezone
IST = pytz.timezone('Asia/Kolkata')

def get_ist_time():
    return datetime.now(IST)

uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)

# Pull credentials from Render Environment Variables
GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_PASS = os.environ.get('GMAIL_PASSWORD') 

def send_smtp_email(to_email, subject, body):
    if not GMAIL_USER or not GMAIL_PASS:
        print("❌ Error: GMAIL_USER or GMAIL_PASSWORD missing in Render.")
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
        print(f"❌ SMTP Error: {str(e)}") 
        return False

# ── Models ────────────────────────────────────────────────────────────────────
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

# ── Routes ────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def index():
    return redirect(url_for('dashboard')) if 'user_id' in session else redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        pw = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(pw):
            session.update({'user_id': user.id, 'user_name': user.name})
            return redirect(url_for('dashboard'))
        flash("Invalid login.", "danger")
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    uid = session.get('user_id')
    meds = Medication.query.filter_by(user_id=uid).all() 
    now_ist = get_ist_time()
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
    return redirect(url_for('dashboard'))

# ── Engine ────────────────────────────────────────────────────────────────────
def send_reminder_task(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med: return
        success = send_smtp_email(med.recipient_email, f"💊 {med.name}", f"Take {med.dose}.")
        log = AlertLog(user_id=med.user_id, medication_name=med.name, status='sent' if success else 'failed', recipient=med.recipient_email)
        db.session.add(log)
        db.session.commit()

def check_and_send():
    with app.app_context():
        now_str = get_ist_time().strftime('%H:%M')
        meds = Medication.query.filter_by(active=True).all()
        for m in meds:
            if m.time1 == now_str or m.time2 == now_str:
                threading.Thread(target=send_reminder_task, args=(m.id,)).start()

with app.app_context():
    db.create_all()

scheduler = BackgroundScheduler(timezone=IST)
scheduler.add_job(check_and_send, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)

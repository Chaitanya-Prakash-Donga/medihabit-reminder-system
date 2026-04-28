"""
MediHabit - app.py
Full Flask backend: auth, CRUD, Gmail SMTP (SSL 465)
Trigger-based reminders for universal timezone support.
"""
import os
import threading
import smtplib
from datetime import datetime
from functools import wraps
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash, send_from_directory, jsonify)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
# Using SECURITY_KEY or SECRET_KEY from environment
app.secret_key = os.environ.get('SECURITY_KEY', os.environ.get('SECRET_KEY', 'medihabit-super-secret-123'))

# Helper: Get current time as a 'naive' object for DB consistency
def get_now_naive():
    return datetime.now().replace(tzinfo=None)

uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)

# ── Gmail SMTP Email Logic (Secure SSL 465) ──────────────────────────────────
def send_smtp_email(to_email, subject, body):
    sender_email = os.environ.get('GMAIL_USER')
    sender_password = os.environ.get('GMAIL_PASSWORD') # 16-character App Password
    
    if not sender_email or not sender_password:
        print("❌ Error: GMAIL_USER or GMAIL_PASSWORD not set")
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = f"MediHabit <{sender_email}>"
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender_email, sender_password)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"❌ Gmail SMTP Error: {str(e)}") 
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
            
            welcome_body = f"Hi {name},\n\nWelcome to MediHabit! Your medicine reminders are now ready."
            threading.Thread(target=send_smtp_email, args=(email, "Welcome! 💊", welcome_body)).start()
            
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
            flash(f"Welcome back, {user.name}!", "success")
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

# ── FIXED: EDIT MEDICATION ROUTE ─────────────────────────────────────────────
@app.route('/medication/edit/<int:medication_id>', methods=['GET', 'POST'])
@login_required
def edit_medication(medication_id):
    med = Medication.query.get_or_404(medication_id)
    
    if med.user_id != session['user_id']:
        return "Unauthorized", 403

    if request.method == 'POST':
        med.name = request.form.get('name')
        med.dose = request.form.get('dose')
        med.time1 = request.form.get('time1')
        med.time2 = request.form.get('time2') or None
        med.recipient_email = request.form.get('recipient_email')
        
        db.session.commit()
        flash("Medication updated!", "success")
        return redirect(url_for('dashboard'))

    return render_template('edit_medication.html', medication=med)

# ── FIXED: EDIT PROFILE ROUTE ────────────────────────────────────────────────
@app.route('/edit_profile', methods=['GET', 'POST'])
@login_required
def edit_profile():
    user = User.query.get(session['user_id'])
    
    if request.method == 'POST':
        user.name = request.form.get('name')
        db.session.commit()
        session['user_name'] = user.name
        flash("Profile updated!", "success")
        return redirect(url_for('dashboard'))

    return render_template('edit_profile.html', user=user)

@app.route('/medication/add', methods=['POST'])
@login_required
def add_medication():
    m = Medication(
        user_id=session['user_id'],
        name=request.form.get('name'),
        dose=request.form.get('dose'),
        time1=request.form.get('time1'),
        time2=request.form.get('time2') or None,
        recipient_email=request.form.get('recipient_email')
    )
    db.session.add(m)
    db.session.commit()
    flash(f'"{m.name}" scheduled!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/medication/delete/<int:id>')
@login_required
def delete_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id == session['user_id']:
        db.session.delete(med)
        db.session.commit()
    return redirect(url_for('dashboard'))

# ── LOCATION-AWARE TRIGGER ROUTE ─────────────────────────────────────────────
@app.route('/trigger-reminder/<int:med_id>', methods=['POST'])
@login_required
def trigger_reminder(med_id):
    """Called by the Browser JS when the local clock matches med time."""
    threading.Thread(target=send_reminder_task, args=(med_id,), daemon=True).start()
    return jsonify({"status": "received"}), 200

def send_reminder_task(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med: return
        
        subject = f"💊 Time for {med.name}"
        body = f"Reminder: It is time to take {med.name} ({med.dose})."
        success = send_smtp_email(med.recipient_email, subject, body)
        
        log = AlertLog(
            user_id=med.user_id, 
            medication_name=med.name, 
            status='sent' if success else 'failed', 
            recipient=med.recipient_email,
            sent_at=get_now_naive()
        )
        db.session.add(log)
        db.session.commit()

# ── PWA & Service Worker ─────────────────────────────────────────────────────
@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js')

# ── Startup ───────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True)

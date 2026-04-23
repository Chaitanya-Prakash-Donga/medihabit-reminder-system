"""
MediHabit - app.py
✅ FIXED: Email sending (Resend API), IST timezone, Alert logs, Scheduler
Full Flask backend: auth, CRUD, Gmail SMTP email alerts, APScheduler
"""
import os
import threading
import resend
import pytz
from datetime import datetime, timedelta
from functools import wraps
import logging

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash, send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'medihabit-super-secret-key-2024')

# Define IST Timezone
IST = pytz.timezone('Asia/Kolkata')

# Database setup
uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True, 
    "pool_recycle": 280,
    "pool_timeout": 20
}

db = SQLAlchemy(app)

# ── ✅ FIXED: Resend API Email Logic ──────────────────────────────────────────
resend.api_key = os.environ.get('RESEND_API_KEY')

def send_email(to_email, subject, body_html="", body_text=None):
    """✅ FIXED: Robust email sending with proper error handling"""
    if not resend.api_key:
        logger.error("❌ RESEND_API_KEY not configured")
        return False
    
    try:
        # Use HTML by default for better formatting
        if not body_html:
            body_html = f"<h2>{subject}</h2><p>{body_text or body_html}</p>"
        
        params = {
            "from": "MediHabit 💊 <onboarding@resend.dev>",
            "to": [to_email],
            "subject": subject,
            "html": body_html,
        }
        
        logger.info(f"📧 Sending email to {to_email}: {subject}")
        result = resend.Emails.send(params)
        logger.info(f"✅ Email sent successfully to {to_email}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Email failed for {to_email}: {str(e)}")
        return False

# ── Models ────────────────────────────────────────────────────────────────────
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(IST))
    medications = db.relationship('Medication', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw, method='pbkdf2:sha256')

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

class Medication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    dose = db.Column(db.String(100))
    frequency = db.Column(db.String(50))
    time1 = db.Column(db.String(5), nullable=False)  # HH:MM format
    time2 = db.Column(db.String(5), nullable=True)
    recipient_email = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.String(300))
    email_enabled = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(IST))

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medication_id = db.Column(db.Integer, db.ForeignKey('medication.id'))
    medication_name = db.Column(db.String(200))
    recipient = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(IST))
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

# ── PWA & Service Worker Routes ───────────────────────────────────────────────
@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js')

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return redirect(url_for('dashboard')) if 'user_id' in session else redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip().lower()
            pw = request.form.get('password', '')
            
            if not all([name, email, pw]):
                flash("Please fill all fields!", "danger")
                return redirect(url_for('register'))
            
            if User.query.filter_by(email=email).first():
                flash("Email already registered!", "danger")
                return redirect(url_for('register'))
            
            user = User(name=name, email=email)
            user.set_password(pw)
            db.session.add(user)
            db.session.commit()
            
            # ✅ FIXED: Send welcome email IMMEDIATELY (not threaded)
            welcome_html = f"""
            <h2>Welcome to MediHabit, {name}! 💊</h2>
            <p>Your account has been created successfully!</p>
            <p><strong>Email:</strong> {email}</p>
            <hr>
            <p>MediHabit will send you timely medication reminders.<br>
            Add your medications in the dashboard.</p>
            """
            
            success = send_email(email, f"Welcome to MediHabit, {name}! 💊", welcome_html)
            logger.info(f"Welcome email sent to {email}: {'✅' if success else '❌'}")
            
            flash("Account created successfully! Please login.", "success")
            return redirect(url_for('login'))
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Registration error: {str(e)}")
            flash(f"Registration failed: {str(e)}", "danger")
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pw = request.form.get('password', '')
        
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(pw):
            session.update({
                'user_id': user.id, 
                'user_name': user.name,
                'user_email': user.email
            })
            flash(f"Welcome back, {user.name}! 👋", "success")
            return redirect(url_for('dashboard'))
        
        flash("Invalid email or password.", "danger")
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("Logged out successfully.", "info")
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    uid = session.get('user_id')
    meds = Medication.query.filter_by(user_id=uid, active=True).order_by(Medication.name).all()
    
    # Voice Alert JS data
    meds_js = [{"name": m.name, "t1": m.time1, "t2": m.time2} for m in meds]

    # ✅ FIXED: Proper IST timezone handling for logs
    now_ist = datetime.now(IST)
    today_ist_date = now_ist.date()
    
    logs = AlertLog.query.filter(
        AlertLog.user_id == uid, 
        db.func.date(AlertLog.sent_at) == today_ist_date
    ).order_by(AlertLog.sent_at.desc()).limit(10).all()
    
    today_display = now_ist.strftime('%A, %d %B %Y • %I:%M %p IST')
    current_time = now_ist.strftime('%H:%M')
    
    return render_template(
        'dashboard.html', 
        meds=meds, 
        meds_js=meds_js, 
        logs=logs, 
        today_date=today_display,
        current_time=current_time
    )

@app.route('/medication/add', methods=['POST'])
@login_required
def add_medication():
    try:
        recipient_email = request.form.get('recipient_email') or session.get('user_email')
        
        m = Medication(
            user_id=session['user_id'],
            name=request.form.get('name', '').strip(),
            dose=request.form.get('dose', ''),
            frequency=request.form.get('frequency', ''),
            time1=request.form.get('time1', ''),
            time2=request.form.get('time2') or None,
            recipient_email=recipient_email,
            notes=request.form.get('notes', '')
        )
        
        if not m.name or not m.time1:
            flash("Medication name and first time are required!", "danger")
            return redirect(url_for('dashboard'))
            
        db.session.add(m)
        db.session.commit()
        flash(f'✅ "{m.name}" added successfully!', 'success')
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Add medication error: {str(e)}")
        flash("Failed to add medication.", "danger")
    
    return redirect(url_for('dashboard'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = User.query.get_or_404(session['user_id'])
    
    if request.method == 'POST':
        try:
            new_name = request.form.get('name', '').strip()
            if new_name:
                user.name = new_name
                session['user_name'] = new_name
            
            new_pw = request.form.get('password', '').strip()
            if new_pw:
                user.set_password(new_pw)
            
            db.session.commit()
            flash("✅ Profile updated successfully!", "success")
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Profile update error: {str(e)}")
            flash("❌ Error updating profile.", "danger")
    
    return render_template('edit_profile.html', user=user)

@app.route('/medication/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id != session['user_id']:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        try:
            med.name = request.form.get('name', med.name)
            med.dose = request.form.get('dose', med.dose)
            med.frequency = request.form.get('frequency', med.frequency)
            med.time1 = request.form.get('time1', med.time1)
            med.time2 = request.form.get('time2') or None
            med.recipient_email = request.form.get('recipient_email') or session.get('user_email')
            med.notes = request.form.get('notes', med.notes)
            
            db.session.commit()
            flash("✅ Medication updated!", "success")
            return redirect(url_for('dashboard'))
        except Exception as e:
            db.session.rollback()
            flash("❌ Error updating medication.", "danger")
    
    return render_template('edit_medication.html', med=med)

@app.route('/medication/delete/<int:id>')
@login_required
def delete_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id == session['user_id']:
        med_name = med.name
        db.session.delete(med)
        db.session.commit()
        flash(f'✅ "{med_name}" removed.', "success")
    return redirect(url_for('dashboard'))

@app.route('/medication/toggle/<int:id>')
@login_required
def toggle_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id == session['user_id']:
        med.active = not med.active
        status = "activated" if med.active else "deactivated"
        db.session.commit()
        flash(f'✅ "{med.name}" {status}.', "success")
    return redirect(url_for('dashboard'))

# ── ✅ FIXED: Reminder Engine (Most Critical Fixes) ───────────────────────────
def send_reminder_task(med_id):
    """✅ FIXED: Proper app context, IST timezone, error logging"""
    try:
        with app.app_context():
            med = Medication.query.get(med_id)
            if not med or not med.active or not med.email_enabled:
                return
            
            now_ist = datetime.now(IST)
            
            # Create beautiful HTML reminder
            reminder_html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto;">
                <h2 style="color: #2c5aa0; background: #e8f4f8; padding: 20px; border-radius: 10px;">
                    ⏰ Medication Reminder
                </h2>
                <div style="background: white; padding: 30px; border-radius: 10px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
                    <h3 style="color: #d63384;">💊 Time for: <strong>{med.name}</strong></h3>
                    <p><strong>Dose:</strong> {med.dose or 'As prescribed'}</p>
                    <p><strong>Time:</strong> {now_ist.strftime('%I:%M %p IST')}</p>
                    <hr style="border: 1px solid #eee;">
                    <p style="color: #666; font-style: italic;">{med.notes or 'Don’t forget to take your medication!'}</p>
                    <div style="text-align: center; margin-top: 20px;">
                        <p>🩺 Stay healthy!</p>
                        <p><em>— MediHabit Team</em></p>
                    </div>
                </div>
            </div>
            """
            
            subject = f"⏰ {med.name} - Time to take your medication!"
            success = send_email(med.recipient_email, subject, reminder_html)
            
            # ✅ FIXED: Log with proper IST timestamp
            log = AlertLog(
                user_id=med.user_id,
                medication_id=med.id,
                medication_name=med.name,
                recipient=med.recipient_email,
                status='sent' if success else 'failed',
                error="" if success else f"Email delivery failed",
                sent_at=now_ist  # ✅ CRITICAL: Explicit IST timestamp
            )
            db.session.add(log)
            db.session.commit()
            
            logger.info(f"Reminder {'✅sent' if success else '❌failed'} for {med.name} to {med.recipient_email}")
            
    except Exception as e:
        logger.error(f"Reminder task error for med_id {med_id}: {str(e)}")

def check_and_send_reminders():
    """✅ FIXED: Precise time matching every minute"""
    try:
        with app.app_context():
            now_ist = datetime.now(IST)
            current_time = now_ist.strftime('%H:%M')  # HH:MM format
            
            logger.info(f"🕐 Checking reminders at {current_time} IST")
            
            active_meds = Medication.query.filter_by(active=True, email_enabled=True).all()
            
            for med in active_meds:
                # ✅ FIXED: Exact time matching
                if med.time1 == current_time or (med.time2 and med.time2 == current_time):
                    logger.info(f"📅 Triggering reminder for {med.name} at {current_time}")
                    # Use daemon thread to avoid hanging
                    threading.Thread(
                        target=send_reminder_task, 
                        args=(med.id,), 
                        daemon=True
                    ).start()
                    
    except Exception as e:
        logger.error(f"Reminder check error: {str(e)}")

# ── Startup & Scheduler ────────────────────────────────────────────────────────
@app.before_first_request
def create_tables():
    db.create_all()

# ✅ FIXED: Proper scheduler setup with IST timezone
def init_scheduler():
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(
        func=check_and_send_reminders,
        trigger='interval',
        minutes=1,
        id='reminder_checker',
        replace_existing=True,
        misfire_grace_time=60
    )
    scheduler.start()
    logger.info("✅ Scheduler started - checking reminders every minute (IST)")
    return scheduler

# Global scheduler
scheduler = None

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    
    scheduler = init_scheduler()
    app.run(debug=True, use_reloader=False, port=5000)

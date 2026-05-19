import os
import random
import threading
from datetime import datetime, timedelta
from functools import wraps

import resend
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECURITY_KEY', 'medihabit-super-secret-123')

# Initialize Resend API Key
resend.api_key = os.environ.get('RESEND_API_KEY')

def get_now_naive():
    return datetime.now().replace(tzinfo=None, microsecond=0)

uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)

# ── Resend Email Function ─────────────────────────────────────────────────────
def send_mail_via_resend(to_email, subject, body):
    try:
        params = {
            "from": "MediHabit <onboarding@resend.dev>",
            "to": [to_email],
            "subject": subject,
            "text": body,
        }
        resend.Emails.send(params)
        print(f"✅ Email sent to {to_email}")
        return True
    except Exception as e:
        print(f"❌ Resend Error: {e}")
        return False

# ── Verification & Onboarding Notification Helpers ───────────────────────────
def send_otp_email(recipient_email, name, otp_code):
    subject = "Verify Your MediHabit Account 🔑"
    body = (f"Hello {name},\n\n"
            f"Thank you for registering with the MediHabit Reminder System!\n\n"
            f"Your 4-digit One-Time Password (OTP) verification code is: {otp_code}\n\n"
            f"Please enter this code on the registration verification screen to activate your profile. "
            f"This code is confidential and should not be shared.\n\n"
            f"Best regards,\nThe MediHabit Team")
    return send_mail_via_resend(recipient_email, subject, body)

def send_welcome_email(recipient_email, name):
    subject = "Welcome to MediHabit! 💊"
    body = (f"Hello {name},\n\n"
            "Thank you for joining the MediHabit Reminder System! "
            "We are here to help you stay on track with your health and medications.\n\n"
            "Log in now to start adding your daily schedules.\n\n"
            "Best regards,\nThe MediHabit Team")
    return send_mail_via_resend(recipient_email, subject, body)

# ── Models ────────────────────────────────────────────────────────────────────
class User(db.Model):
    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=get_now_naive)
    
    # Registration and Verification States
    mobile_number = db.Column(db.String(15), unique=True, nullable=False)
    otp = db.Column(db.String(4), nullable=True)  
    is_verified = db.Column(db.Boolean, default=False)
    
    medications = db.relationship('Medication', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

class Medication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    dose = db.Column(db.String(100))
    time1 = db.Column(db.String(5))
    time2 = db.Column(db.String(5), nullable=True)
    recipient_email = db.Column(db.String(120))
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)
    email_enabled = db.Column(db.Boolean, default=True)

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    medication_name = db.Column(db.String(200))
    recipient = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime, default=get_now_naive)
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

        success = send_mail_via_resend(med.recipient_email, subject, body)

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
    with app.app_context():
        now = get_now_naive()
        now_str = now.strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for m in meds:
            if m.time1 == now_str or m.time2 == now_str:
                recent_log = AlertLog.query.filter(
                    AlertLog.user_id == m.user_id,
                    AlertLog.medication_name == m.name,
                    AlertLog.sent_at >= now - timedelta(seconds=59)
                ).first()
                
                if not recent_log:
                    threading.Thread(target=send_reminder_task, args=(m.id,), daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return redirect(url_for('dashboard')) if 'user_id' in session else redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            username = request.form.get('username') or request.form.get('name')
            email = request.form.get('email').strip().lower()
            password = request.form.get('password')
            mobile = request.form.get('mobile') or request.form.get('mobile_number')
            
            # Check if email already exists BEFORE trying to add it
            if User.query.filter_by(email=email).first():
                flash("Email already registered! Please log in.", "danger")
                return redirect(url_for('register'))
                
            # Check if mobile already exists BEFORE trying to add it
            existing_user = User.query.filter_by(mobile_number=mobile).first()
            if existing_user:
                flash("Mobile number already registered. Please log in.", "danger")
                return redirect(url_for('register'))
                
            generated_otp = str(random.randint(1000, 9999))
            
            new_user = User(
                name=username,
                email=email,
                mobile_number=mobile,
                otp=generated_otp,
                is_verified=False
            )
            new_user.set_password(password)
            
            db.session.add(new_user)
            db.session.commit()  # Complete structural commit
            
            session['pending_mobile'] = mobile 
            
            print(f"DEBUG DIAGNOSTIC: OTP generated for {mobile} / {email} is -> [{generated_otp}]")
            
            # Async background notification thread dispatching security code
            try:
                threading.Thread(
                    target=send_otp_email, 
                    args=(email, username, generated_otp), 
                    daemon=True
                ).start()
            except Exception as mail_err:
                print(f"⚠️ Non-blocking background registration email error: {mail_err}")

            flash("OTP sent to your email address!", "success")
            return redirect(url_for('verify_otp')) 
            
        except Exception as e:
            db.session.rollback()
            flash(f"Registration Interruption Error: {str(e)}", "danger")
            return redirect(url_for('register'))
            
    return render_template('register.html')

@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    mobile = session.get('pending_mobile')
    if not mobile:
        flash("No active registration wizard session detected. Please sign up.", "warning")
        return redirect(url_for('register'))
        
    if request.method == 'POST':
        user_otp_input = request.form.get('otp')
        user = User.query.filter_by(mobile_number=mobile).first()
        
        if user and user.otp == user_otp_input:
            try:
                user.is_verified = True
                user.otp = None  # Clear OTP string directly upon successful match
                db.session.commit()
                
                saved_email = user.email
                saved_name = user.name
                
                session.pop('pending_mobile', None)
                
                try:
                    threading.Thread(
                        target=send_welcome_email, 
                        args=(saved_email, saved_name), 
                        daemon=True
                    ).start()
                except Exception as welcome_err:
                    print(f"⚠️ Non-blocking background welcome email error: {welcome_err}")
                
                flash("Account verified successfully! You can now log in.", "success")
                return redirect(url_for('login'))
                
            except Exception as commit_err:
                db.session.rollback()
                flash(f"Database sync validation issue: {str(commit_err)}", "danger")
        else:
            flash("Invalid OTP. Please try again.", "danger")
            
    return render_template('verify_otp.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        pw = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        
        if user and user.check_password(pw):
            if not user.is_verified:
                session['pending_mobile'] = user.mobile_number
                flash("Your account status is currently unverified. Please confirm validation code.", "warning")
                return redirect(url_for('verify_otp'))
                
            session.update({'user_id': user.id, 'user_name': user.name, 'user_email': user.email})
            return redirect(url_for('dashboard'))
            
        flash("Invalid structural validation credentials.", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    uid = session.get('user_id')
    user = User.query.get(uid)
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
                           user=user,
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

@app.route('/medication/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_medication(id):
    med = Medication.query.get_or_404(id) 
    
    if med.user_id != session['user_id']:
        abort(403)
        
    if request.method == 'POST':
        med.name = request.form.get('name')
        med.dose = request.form.get('dose')
        med.time1 = request.form.get('time1')
        med.time2 = request.form.get('time2') or None 
        med.recipient_email = request.form.get('recipient_email')
        med.notes = request.form.get('notes')
        med.email_enabled = 'email_enabled' in request.form
        
        db.session.commit()
        flash(f'"{med.name}" updated!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('edit_medication.html', med=med)

@app.route('/medication/delete/<int:id>', methods=['POST'])
@login_required
def delete_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id != session['user_id']:
        abort(403)
    db.session.delete(med)
    db.session.commit()
    flash("Medication deleted.", "success")
    return redirect(url_for('dashboard'))

@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = User.query.get_or_404(session['user_id'])
    if request.method == 'POST':
        user.name = request.form.get('name')
        new_pw = request.form.get('password')
        if new_pw:
            user.set_password(new_pw)
        db.session.commit()
        session['user_name'] = user.name
        flash("Profile updated!", "success")
        return redirect(url_for('dashboard'))
    return render_template('edit_profile.html', user=user)

@app.route('/trigger-reminder/<int:med_id>', methods=['POST'])
@login_required
def trigger_reminder(med_id):
    med = Medication.query.get(med_id)
    if not med: 
        return jsonify({"status": "not_found"}), 404
    
    now = get_now_naive()
    recent_log = AlertLog.query.filter(
        AlertLog.user_id == session['user_id'],
        AlertLog.medication_name == med.name,
        AlertLog.sent_at >= now - timedelta(seconds=59)
    ).first()

    if recent_log:
        return jsonify({"status": "already_sent_this_minute"}), 200

    new_log = AlertLog(
        user_id=session['user_id'], 
        medication_name=med.name,
        status='pending', 
        recipient=med.recipient_email, 
        sent_at=now
    )
    db.session.add(new_log)
    db.session.commit()
    
    threading.Thread(target=send_reminder_task, args=(med.id, new_log.id), daemon=True).start()
    return jsonify({"status": "received"}), 200

# ── Startup & Scheduler ───────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

scheduler = BackgroundScheduler()
if not scheduler.running:
    scheduler.add_job(check_and_send, 'interval', minutes=1, id='med_job', replace_existing=True)
    scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)

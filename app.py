import os
import random
import smtplib
import threading
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECURITY_KEY', 'medihabit-super-secret-123')

def get_now_naive():
    return datetime.now().replace(tzinfo=None, microsecond=0)

uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)

# ── Gmail SMTP Configuration ──────────────────────────────────────────────────
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'your-gmail-username@gmail.com')  
SENDER_PASSWORD = os.environ.get('SENDER_PASSWORD', 'your-16-digit-app-password')  

# ── HTML Email Core Function ──────────────────────────────────────────────────
def send_html_email(receiver_email, subject, html_body):
    msg = MIMEMultipart('alternative')
    msg['From'] = f"MediHabit <{SENDER_EMAIL}>"
    msg['To'] = receiver_email
    msg['Subject'] = subject
    
    msg.attach(MIMEText(html_body, 'html'))
    
    try:
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, receiver_email, msg.as_string())
        server.quit()
        print(f"✅ HTML Email sent successfully to {receiver_email}")
        return True
    except Exception as e:
        print(f"❌ SMTP Mail Delivery Error: {e}")
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
    time1 = db.Column(db.String(5))
    time2 = db.Column(db.String(5), nullable=True)
    recipient_email = db.Column(db.String(120))
    notes = db.Column(db.Text)
    active = db.Column(db.Boolean, default=True)
    email_enabled = db.Column(db.Boolean, default=True)

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
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

        subject = f"💊 Time for your medication: {med.name}"
        html_body = f"""
        <html>
            <body style="font-family: Arial, sans-serif; background-color: #f4f6f9; padding: 20px; color: #333;">
                <div style="max-width: 600px; margin: 0 auto; background: #ffffff; padding: 30px; border-top: 4px solid #007bff; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
                    <h2 style="color: #007bff; text-align: center; margin-top: 0;">Medication Reminder</h2>
                    <p>Hello,</p>
                    <p>This is an automated notification from your <strong>MediHabit System</strong> to remind you that it is time to take your dose.</p>
                    <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                        <tr style="background-color: #f8f9fa;">
                            <td style="padding: 10px; border: 1px solid #dee2e6; font-weight: bold; width: 30%;">Medication:</td>
                            <td style="padding: 10px; border: 1px solid #dee2e6;">{med.name}</td>
                        </tr>
                        <tr>
                            <td style="padding: 10px; border: 1px solid #dee2e6; font-weight: bold;">Dosage:</td>
                            <td style="padding: 10px; border: 1px solid #dee2e6;">{med.dose if med.dose else 'N/A'}</td>
                        </tr>
                        <tr style="background-color: #f8f9fa;">
                            <td style="padding: 10px; border: 1px solid #dee2e6; font-weight: bold;">Notes:</td>
                            <td style="padding: 10px; border: 1px solid #dee2e6;">{med.notes if med.notes else 'None'}</td>
                        </tr>
                    </table>
                    <p style="font-size: 13px; color: #6c757d; text-align: center; margin-top: 30px;">
                        Stay healthy!<br>Generated by MediHabit Tracker.
                    </p>
                </div>
            </body>
        </html>
        """

        success = send_html_email(med.recipient_email, subject, html_body)

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
            name = request.form.get('name')
            email = request.form.get('email').strip().lower()
            pw = request.form.get('password')
            
            if User.query.filter_by(email=email).first():
                flash("Email already registered!", "danger")
                return redirect(url_for('register'))
            
            # Generate 4-digit verification code
            otp = str(random.randint(1000, 9999))
            
            # Put registration data onto temporary user session
            session['pending_user'] = {
                'name': name,
                'email': email,
                'password': pw
            }
            session['otp'] = otp
            
            # Create Custom HTML Email Body
            html_content = f"""
            <html>
                <body style="font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px;">
                    <div style="max-width: 500px; margin: 0 auto; background: #fff; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
                        <h2 style="color: #28a745; text-align: center;">MediHabit Verification</h2>
                        <p>Hello {name},</p>
                        <p>Thank you for signing up for MediHabit. Please enter the following 4-digit verification code to fully activate your profile:</p>
                        <div style="font-size: 32px; font-weight: bold; text-align: center; letter-spacing: 8px; margin: 24px 0; padding: 15px; background: #f8f9fa; border: 1px dashed #28a745; color: #28a745; border-radius: 4px;">
                            {otp}
                        </div>
                        <p style="font-size: 12px; color: #6c757d; text-align: center; margin-top: 30px;">
                            If you did not execute this profile request, you can safely ignore this notification.
                        </p>
                    </div>
                </body>
            </html>
            """
            
            # Execute mailing using threaded background task
            subject = "MediHabit - Verify Your Account 💊"
            threading.Thread(target=send_html_email, args=(email, subject, html_content), daemon=True).start()

            flash("A 4-digit OTP has been dispatched to your email address.", "info")
            return redirect(url_for('verify_otp'))
            
        except Exception as e:
            flash(f"Registration Error: {str(e)}", "danger")
            
    return render_template('register.html')

@app.route('/verify-otp', methods=['GET', 'POST'])
def verify_otp():
    if 'pending_user' not in session or 'otp' not in session:
        flash("No valid registration process found.", "warning")
        return redirect(url_for('register'))
        
    if request.method == 'POST':
        user_otp = request.form.get('otp')
        
        if user_otp == session['otp']:
            try:
                user_data = session['pending_user']
                
                # Instantiating user data inside database
                user = User(name=user_data['name'], email=user_data['email'])
                user.set_password(user_data['password'])
                
                db.session.add(user)
                db.session.commit()
                
                # Clear out registration session data caches
                session.pop('pending_user', None)
                session.pop('otp', None)
                
                flash("Account verified successfully! You can now log in.", "success")
                return redirect(url_for('login'))
                
            except Exception as e:
                db.session.rollback()
                flash(f"Database error writing user context: {str(e)}", "danger")
        else:
            flash("Invalid OTP match. Please try again.", "danger")
            
    return render_template('verify_otp.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        pw = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(pw):
            session.update({'user_id': user.id, 'user_name': user.name, 'user_email': user.email})
            return redirect(url_for('dashboard'))
        flash("Invalid credentials", "danger")
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
    if not med: return jsonify({"status": "not_found"}), 404
    
    now = get_now_naive()
    recent_log = AlertLog.query.filter(
        AlertLog.user_id == session['user_id'],
        AlertLog.medication_name == med.name,
        AlertLog.sent_at >= now - timedelta(seconds=59)
    ).first()

    if recent_log:
        return jsonify({"status": "already_sent_this_minute"}), 200

    new_log = AlertLog(
        user_id=session['user_id'], medication_name=med.name,
        status='pending', recipient=med.recipient_email, sent_at=now
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

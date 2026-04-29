import os
import threading
import smtplib
from datetime import datetime, timedelta
from functools import wraps
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash, send_from_directory, jsonify)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECURITY_KEY', 'medihabit-secret-999')

# Replace with your actual deployed URL for email links
BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5000')

def get_now_naive():
    """Returns the current time without timezone info for database compatibility."""
    return datetime.now().replace(tzinfo=None)

# Database Configuration
uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ── Email Styling ─────────────────────────────────────────────────────────────
EMAIL_BTN_STYLE = "color: #ffffff; background-color: #2ecc71; padding: 12px 25px; text-decoration: none; border-radius: 5px; font-weight: bold; display: inline-block;"

# ── Core Email Function ───────────────────────────────────────────────────────
def send_smtp_email(to_email, subject, html_body):
    sender_email = os.environ.get('GMAIL_USER')
    sender_password = os.environ.get('GMAIL_PASSWORD') # Must be a 16-character App Password
    
    if not sender_email or not sender_password:
        print("❌ SMTP Error: GMAIL_USER or GMAIL_PASSWORD environment variables are missing.")
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = f"MediHabit Reminder <{sender_email}>"
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender_email, sender_password)
            server.send_message(msg)
        print(f"✅ Email successfully sent to {to_email}")
        return True
    except Exception as e:
        print(f"❌ SMTP Failed: {str(e)}") 
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
    time1 = db.Column(db.String(5))   # HH:MM format
    time2 = db.Column(db.String(5), nullable=True)
    recipient_email = db.Column(db.String(120))
    notes = db.Column(db.String(300))

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medication_name = db.Column(db.String(200))
    recipient = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime, nullable=False) 
    status = db.Column(db.String(20))

# ── Auth Decorator ────────────────────────────────────────────────────────────
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
        name = request.form.get('name')
        email = request.form.get('email').strip().lower()
        pw = request.form.get('password')
        
        if User.query.filter_by(email=email).first():
            flash("This email is already registered.", "danger")
            return redirect(url_for('register'))
        
        user = User(name=name, email=email)
        user.set_password(pw)
        db.session.add(user)
        db.session.commit()
        
        # Send Welcome Email
        welcome_html = f"""
        <div style="font-family: sans-serif; padding: 20px; border: 1px solid #eee; border-radius: 10px;">
            <h2 style="color: #2c3e50;">Welcome to MediHabit, {name}! 💊</h2>
            <p>Thank you for joining us. We are here to help you stay on track with your health routines.</p>
            <p>Your account is now active. You can start adding your medications and setting your daily alert times immediately.</p>
            <div style="margin: 20px 0;">
                <a href="{BASE_URL}" style="{EMAIL_BTN_STYLE}">Go to Dashboard</a>
            </div>
            <p style="color: #7f8c8d; font-size: 12px;">If you didn't create this account, please ignore this email.</p>
        </div>
        """
        threading.Thread(target=send_smtp_email, args=(email, "Welcome to MediHabit! 💊", welcome_html)).start()
        
        flash("Registration successful! Please login.", "success")
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        pw = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(pw):
            session.update({'user_id': user.id, 'user_name': user.name, 'user_email': user.email})
            return redirect(url_for('dashboard'))
        flash("Invalid credentials.", "danger")
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
    # Prepare data for the JavaScript local-time checker
    meds_js = [{"id": m.id, "name": m.name, "t1": m.time1, "t2": m.time2} for m in meds]
    
    today_date = get_now_naive().date()
    logs = AlertLog.query.filter(
        AlertLog.user_id == uid, 
        db.func.date(AlertLog.sent_at) == today_date
    ).order_by(AlertLog.sent_at.desc()).all()
    
    return render_template('dashboard.html', meds=meds, meds_js=meds_js, logs=logs, 
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
        notes=request.form.get('notes')
    )
    db.session.add(m)
    db.session.commit()
    flash(f"'{m.name}' added successfully!", "success")
    return redirect(url_for('dashboard'))

@app.route('/medication/delete/<int:id>')
@login_required
def delete_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id == session['user_id']:
        db.session.delete(med)
        db.session.commit()
        flash("Medication removed.", "info")
    return redirect(url_for('dashboard'))

@app.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    user = User.query.get(session['user_id'])
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

# ── Reminder Trigger Logic ────────────────────────────────────────────────────
@app.route('/trigger-reminder/<int:med_id>', methods=['POST'])
@login_required
def trigger_reminder(med_id):
    """Triggered by JavaScript when local time matches scheduled time."""
    # Cooldown to prevent duplicate triggers in the same minute
    cooldown = get_now_naive() - timedelta(seconds=50)
    
    med = Medication.query.get(med_id)
    if not med: return jsonify({"status": "not_found"}), 404

    recent = AlertLog.query.filter(
        AlertLog.user_id == session['user_id'],
        AlertLog.medication_name == med.name,
        AlertLog.sent_at >= cooldown
    ).first()

    if recent:
        return jsonify({"status": "cooldown_active"}), 200

    # Start email task in background
    threading.Thread(target=send_reminder_task, args=(med_id,), daemon=True).start()
    return jsonify({"status": "sent"}), 200

def send_reminder_task(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med: return
        
        subject = f"🔔 Reminder: Time to take {med.name}"
        instructions = med.notes if med.notes else "Take as directed."
        
        email_html = f"""
        <div style="font-family: sans-serif; border: 2px solid #3498db; padding: 20px; border-radius: 15px; max-width: 500px;">
            <h2 style="color: #e67e22;">💊 Medication Reminder</h2>
            <p>It is time for your scheduled dose of <strong>{med.name}</strong>.</p>
            <div style="background: #f9f9f9; padding: 15px; border-radius: 8px; margin: 15px 0;">
                <p><strong>Dosage:</strong> {med.dose}</p>
                <p><strong>Notes:</strong> {instructions}</p>
            </div>
            <p>Please log in to your dashboard to confirm.</p>
            <div style="text-align: center; margin-top: 20px;">
                <a href="{BASE_URL}" style="{EMAIL_BTN_STYLE}">Open MediHabit</a>
            </div>
        </div>
        """
        
        success = send_smtp_email(med.recipient_email, subject, email_html)
        
        log = AlertLog(
            user_id=med.user_id,
            medication_name=med.name,
            recipient=med.recipient_email,
            sent_at=get_now_naive(),
            status="sent" if success else "failed"
        )
        db.session.add(log)
        db.session.commit()

# ── PWA & App Startup ─────────────────────────────────────────────────────────
@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js')

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)

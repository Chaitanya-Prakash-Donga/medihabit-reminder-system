"""
MediHabit - app.py (Updated)
Fixed: Duplicate email logic and enhanced HTML email templates.
"""
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
app.secret_key = os.environ.get('SECURITY_KEY', 'medihabit-super-secret-123')

def get_now_naive():
    return datetime.now().replace(tzinfo=None)

uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ── Enhanced HTML Email Logic ────────────────────────────────────────────────
def send_smtp_email(to_email, subject, html_body):
    sender_email = os.environ.get('GMAIL_USER')
    sender_password = os.environ.get('GMAIL_PASSWORD')
    
    if not sender_email or not sender_password:
        print("❌ Error: GMAIL_USER or GMAIL_PASSWORD not set")
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = f"MediHabit <{sender_email}>"
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(html_body, 'html')) # Changed to 'html'

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
            
            # ── 1. ENHANCED WELCOME MAIL (HTML) ──
            welcome_html = f"""
            <html>
                <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                    <h2 style="color: #2c3e50;">Welcome to Your Health & Habit Companion!</h2>
                    <p>Hello <strong>{name}</strong>,</p>
                    <p>Welcome to your personalized dashboard! We are excited to help you manage your wellness journey with precision. Your account is now fully configured to keep you on schedule and mindful of your health goals.</p>
                    <h3>Getting Started:</h3>
                    <ul>
                        <li><strong>Add Your Schedule:</strong> Enter your medications, dosages, and specific times.</li>
                        <li><strong>Enable Notifications:</strong> Ensure your email and browser alerts are active so you never miss a dose.</li>
                        <li><strong>Track Progress:</strong> View your habit history to stay motivated and consistent.</li>
                    </ul>
                    <p>We are here to support your routine every step of the way. If you have any questions, simply reply to this email.</p>
                    <p>Best regards,<br><strong>The MediHabit Team</strong></p>
                </body>
            </html>
            """
            threading.Thread(target=send_smtp_email, args=(email, "Welcome to MediHabit! 💊", welcome_html)).start()
            
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
        notes=request.form.get('notes') # Ensure notes are saved
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

# ── UPDATED TRIGGER ROUTE WITH DUPLICATE PREVENTION ──────────────────────────
@app.route('/trigger-reminder/<int:med_id>', methods=['POST'])
@login_required
def trigger_reminder(med_id):
    # ── 1. PREVENT DUPLICATES (Database Lock) ──
    # Check if an alert was sent for this medicine in the last 2 minutes
    cooldown_period = datetime.now() - timedelta(minutes=2)
    recent_log = AlertLog.query.filter(
        AlertLog.user_id == session['user_id'],
        AlertLog.medication_name == Medication.query.get(med_id).name,
        AlertLog.sent_at >= cooldown_period
    ).first()

    if recent_log:
        return jsonify({"status": "already_sent_recently"}), 200

    threading.Thread(target=send_reminder_task, args=(med_id,), daemon=True).start()
    return jsonify({"status": "received"}), 200

def send_reminder_task(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med: return
        
        # ── 3. ENHANCED REMINDER MAIL (HTML WITH NOTE) ──
        subject = f"Action Required: Medication Reminder for {med.name}"
        
        # Handle empty notes
        display_note = med.notes if med.notes else "No specific instructions provided."
        
        reminder_html = f"""
        <html>
            <body style="font-family: Arial, sans-serif; color: #333;">
                <h2 style="color: #d35400;">Health Alert: Medication Due Now</h2>
                <p>This is a scheduled notification from your tracker to ensure you stay consistent with your regimen.</p>
                <div style="background-color: #f9f9f9; padding: 15px; border-left: 5px solid #3498db;">
                    <p><strong>Medication Details:</strong></p>
                    <ul>
                        <li><strong>Name:</strong> {med.name}</li>
                        <li><strong>Dosage:</strong> {med.dose}</li>
                    </ul>
                    <p><strong>Important Note:</strong></p>
                    <blockquote style="font-style: italic; color: #555;">
                        "{display_note}"
                    </blockquote>
                </div>
                <h3>Next Steps:</h3>
                <p>Please take your dose as soon as possible. Once completed, log in to your dashboard to mark this as "Taken." This helps maintain your accuracy for your health report.</p>
                <p><em>Stay healthy and stay on track!</em></p>
            </body>
        </html>
        """
        
        success = send_smtp_email(med.recipient_email, subject, reminder_html)
        
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
    # ── 1. STOP DUPLICATES FROM RELOADER ──
    # Disable the reloader to prevent the background threads from starting twice
    app.run(debug=True, use_reloader=False)

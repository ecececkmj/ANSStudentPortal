import os
import random
import string
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3
import re
import time
import uuid
import unicodedata
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from setup_db import setup_database
import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'tools'))   # sync_masterlist.py may live in the 'tools' folder
sys.path.insert(0, _HERE)                          # ...or next to app.py - both work
from sync_masterlist import sync_masterlist, sync_staff, start_watcher

# 1. Import the Cloudflare tool
from flask_cloudflared import run_with_cloudflared

load_dotenv()
app = Flask(__name__)

# 2. Wrap your app with Cloudflare immediately after creating it
run_with_cloudflared(app)

app.secret_key = os.getenv('FLASK_SECRET_KEY')
if not app.secret_key or len(app.secret_key) < 32:
    raise RuntimeError('FLASK_SECRET_KEY is missing or too short in the .env file (use at least 32 random characters).')

# Session cookie safety
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'          # blocks cross-site form attacks (CSRF)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)   # auto log out after 8 hours
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024    # uploads bigger than 16 MB are refused

# ... the rest of your routes go here ...

# Configure File Uploads
UPLOAD_FOLDER = os.path.join('static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

setup_database()
sync_masterlist()      # reads masterlist.xlsx into the allowed_lrns table every time the site starts
sync_staff()           # reads authorized_staff.xlsx (teachers / SSLG who may sign up)
start_watcher()        # keeps watching the Excel file: save it and the site updates by itself

_c = sqlite3.connect('database.db')
_c.execute('CREATE TABLE IF NOT EXISTS event_reactions (student_id INTEGER, event_id INTEGER, action TEXT, PRIMARY KEY (student_id, event_id, action))')
_c.commit()
_c.close()

def get_db_connection():
    conn = sqlite3.connect('database.db')
    conn.row_factory = sqlite3.Row
    return conn

# --- SECURITY HELPERS ---
IMAGE_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
DOC_EXT = IMAGE_EXT | {'pdf', 'doc', 'docx', 'ppt', 'pptx', 'xls', 'xlsx', 'txt', 'csv', 'zip'}

def save_upload(file, allowed):
    """Saves an uploaded file under a unique name. Returns the saved name, or None if the file type is not allowed."""
    if not file or not file.filename:
        return None
    name = secure_filename(file.filename)
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    if ext not in allowed:
        return None
    new_name = uuid.uuid4().hex[:10] + '_' + name      # unique, so nobody can overwrite another person's file
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], new_name))
    return new_name

FAILED_ATTEMPTS = {}       # remembers recent failed logins in memory
LOCK_SECONDS = 10 * 60     # 10 minutes

def client_ip():
    return request.headers.get('CF-Connecting-IP') or request.remote_addr or 'unknown'

def is_locked(key, limit):
    now = time.time()
    recent = [t for t in FAILED_ATTEMPTS.get(key, []) if now - t < LOCK_SECONDS]
    FAILED_ATTEMPTS[key] = recent
    return len(recent) >= limit

def record_fail(key):
    FAILED_ATTEMPTS.setdefault(key, []).append(time.time())

def clear_fails(key):
    FAILED_ATTEMPTS.pop(key, None)

def admin_required():
    """Returns None if the logged-in person is the Master Admin, otherwise a response to send back."""
    if 'teacher_id' not in session:
        return redirect(url_for('teacher_login'))
    conn = get_db_connection()
    user = conn.execute('SELECT is_admin FROM teachers WHERE id = ?', (session['teacher_id'],)).fetchone()
    conn.close()
    if not user or user['is_admin'] != 1:
        return "Access Denied: Master Admins Only", 403
    return None

def is_admin_session(conn):
    row = conn.execute('SELECT is_admin FROM teachers WHERE id = ?', (session.get('teacher_id'),)).fetchone()
    return bool(row and row['is_admin'] == 1)

def teacher_owns_quiz(conn, quiz_id):
    row = conn.execute('SELECT 1 FROM quizzes WHERE id = ? AND teacher_id = ?', (quiz_id, session['teacher_id'])).fetchone()
    return row is not None

def teacher_owns_subject(conn, subject_id):
    row = conn.execute('SELECT 1 FROM subjects WHERE id = ? AND teacher_id = ?', (subject_id, session['teacher_id'])).fetchone()
    return row is not None

def staff_is_allowed(staff_id):
    """Returns the person's CURRENT role from the Excel list ('teacher' / 'sslg'), or None if they were removed."""
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT role FROM allowed_staff WHERE staff_id = ?', (staff_id,)).fetchone()
        return row['role'] if row else None
    except sqlite3.OperationalError:
        return True      # list not created yet: do not lock anybody out
    finally:
        conn.close()

# SSLG accounts can ONLY use these pages (post events / announcements). Everything else is refused.
SSLG_ALLOWED = {'teacher_dashboard', 'add_announcement', 'add_event', 'delete_event', 'delete_announcement', 'api_latest',
                'logout', 'teacher_login', 'teacher_register', 'login', 'register'}

@app.before_request
def staff_guard():
    if 'teacher_id' not in session or request.endpoint in (None, 'static'):
        return None
    sid = session.get('staff_id')
    if sid:
        role = staff_is_allowed(sid)
        if not role:
            # Removed from the Excel list = access ends
            session.clear()
            flash('Your access has been removed. Please contact the administrator.')
            return redirect(url_for('teacher_login'))
        if isinstance(role, str):
            session['teacher_role'] = role          # a role changed in Excel applies immediately
    if session.get('teacher_role') == 'sslg' and request.endpoint not in SSLG_ALLOWED:
        return 'Access Denied: SSLG accounts can only post announcements and events.', 403
    return None

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    return response

def generate_subject_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

# --- AUTHENTICATION ROUTES ---
@app.route('/', methods=['GET', 'POST'])
def login():
    if 'student_id' in session: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        lrn = request.form.get('lrn', '').strip()
        password = request.form.get('password', '')
        acct_key, ip_key = 's:' + lrn, 'ip:' + client_ip()

        if is_locked(acct_key, 5) or is_locked(ip_key, 40):
            flash('Too many failed attempts. Please wait 10 minutes and try again.')
            return render_template('login.html')

        conn = get_db_connection()
        student = conn.execute('SELECT * FROM students WHERE lrn = ?', (lrn,)).fetchone()
        conn.close()

        if student and check_password_hash(student['password_hash'], password):
            clear_fails(acct_key)
            if student['is_approved'] == 0:
                flash('Account pending teacher approval.')
            else:
                session.clear()
                session.permanent = True
                session['student_id'] = student['id']
                session['full_name'] = student['full_name']
                return redirect(url_for('dashboard'))
        else:
            record_fail(acct_key); record_fail(ip_key)
            flash('Invalid LRN or password')
    return render_template('login.html')

# --- SIGN UP: automatic Grade 11 check against the masterlist (allowed_lrns table) ---
def normalize_name(text):
    """Ignores capital letters, accents (Nina = Nina), dots and extra spaces."""
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r'[^a-z0-9 ]', ' ', text.lower())
    return ' '.join(text.split())

def normalize_section(text):
    """11-Carina, 11 carina and 11carina are treated as the same."""
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())

# The website is for Grade 11 students. These sections are allowed too (the research group, Grade 12).
EXCEPTION_SECTIONS = {'12leda'}

def is_allowed_record(record):
    """Grade 11 student, or a member of an exception section (e.g. 12-Leda)."""
    return bool(record) and (record['grade_level'] == 11 or normalize_section(record['section']) in EXCEPTION_SECTIONS)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        lrn = request.form.get('lrn', '').strip()
        full_name = request.form.get('full_name', '')
        section = request.form.get('section', '')
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        if not (lrn.isascii() and lrn.isdigit() and len(lrn) == 12):
            flash('LRN must be exactly 12 digits.')
            return render_template('register.html')
        if is_locked('reg:' + client_ip(), 15):
            flash('Too many failed attempts. Please wait 10 minutes and try again.')
            return render_template('register.html')
        if len(password) < 6:
            flash('Password must be at least 6 characters.')
            return render_template('register.html')
        if password != confirm:
            flash('Passwords do not match.')
            return render_template('register.html')

        conn = get_db_connection()
        try:
            record = conn.execute('SELECT * FROM allowed_lrns WHERE lrn = ?', (lrn,)).fetchone()
        except sqlite3.OperationalError:
            conn.close()
            flash('Sign up is not ready yet. Please tell your teacher.')
            return render_template('register.html')

        # Only Grade 11 students on the masterlist, with matching name AND section, can sign up.
        # One shared message, so nobody can use this form to find out which LRNs are on the list.
        if (not is_allowed_record(record)
                or normalize_name(full_name) != normalize_name(record['full_name'])
                or normalize_section(section) != normalize_section(record['section'])):
            conn.close()
            record_fail('reg:' + client_ip())
            flash('We could not verify your details. Please check your LRN, full name and section (Grade 11 students only).')
            return render_template('register.html')

        if conn.execute('SELECT 1 FROM students WHERE lrn = ?', (lrn,)).fetchone():
            conn.close()
            flash('This LRN already has an account. Please log in.')
            return render_template('register.html')

        hashed_pw = generate_password_hash(password)
        try:
            # Verified automatically, so the account is approved right away (is_approved = 1)
            cur = conn.execute('INSERT INTO students (lrn, username, password_hash, full_name, is_approved) VALUES (?, ?, ?, ?, 1)',
                               (lrn, lrn, hashed_pw, record['full_name']))
            conn.commit()
            # Log the new student in right away, so they go straight to the dashboard
            session.clear()
            session.permanent = True
            session['student_id'] = cur.lastrowid
            session['full_name'] = record['full_name']
            return redirect(url_for('dashboard'))
        except sqlite3.IntegrityError:
            flash('This LRN already has an account. Please log in.')
        finally:
            conn.close()
    return render_template('register.html')

@app.route('/teacher', methods=['GET', 'POST'])
def teacher_login():
    if 'teacher_id' in session: return redirect(url_for('teacher_dashboard'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        acct_key, ip_key = 't:' + username.lower(), 'ip:' + client_ip()

        if is_locked(acct_key, 5) or is_locked(ip_key, 40):
            flash('Too many failed attempts. Please wait 10 minutes and try again.')
            return render_template('teacher_login.html')

        conn = get_db_connection()
        teacher = conn.execute('SELECT * FROM teachers WHERE username = ?', (username,)).fetchone()
        if not teacher:
            # Staff who signed up with an access code can log in with their FULL NAME (only if that name is unique)
            same_name = [t for t in conn.execute('SELECT * FROM teachers WHERE full_name IS NOT NULL').fetchall()
                         if normalize_name(t['full_name']) == normalize_name(username)]
            if len(same_name) == 1:
                teacher = same_name[0]
        conn.close()
        if teacher and check_password_hash(teacher['password_hash'], password):
            clear_fails(acct_key)
            staff_id = teacher['staff_id']
            listed_role = staff_is_allowed(staff_id) if staff_id else None
            if staff_id and not listed_role:
                flash('Your access has been removed. Please contact the administrator.')
                return render_template('teacher_login.html')
            session.clear()
            session.permanent = True
            session['teacher_id'] = teacher['id']
            session['t_username'] = teacher['full_name'] or teacher['username']
            session['teacher_role'] = listed_role if isinstance(listed_role, str) else (teacher['role'] or 'teacher')
            if staff_id:
                session['staff_id'] = staff_id
            return redirect(url_for('teacher_dashboard'))
        else:
            record_fail(acct_key); record_fail(ip_key)
            flash('Invalid credentials')
    return render_template('teacher_login.html')

# --- SIGN UP for authorized teachers / SSLG: Full Name + the Access Code given to them (authorized_staff.xlsx) ---
@app.route('/teacher/register', methods=['GET', 'POST'])
def teacher_register():
    if 'teacher_id' in session: return redirect(url_for('teacher_dashboard'))
    if request.method == 'POST':
        full_name = request.form.get('full_name', '')
        code = request.form.get('access_code', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm_password', '')

        if is_locked('sreg:' + client_ip(), 15):
            flash('Too many failed attempts. Please wait 10 minutes and try again.')
            return render_template('teacher_register.html')
        if not full_name.strip() or not code:
            flash('Please fill in your full name and your access code.')
            return render_template('teacher_register.html')
        if len(password) < 8:
            flash('Password must be at least 8 characters.')
            return render_template('teacher_register.html')
        if password != confirm:
            flash('Passwords do not match.')
            return render_template('teacher_register.html')

        conn = get_db_connection()
        try:
            rec = conn.execute('SELECT * FROM allowed_staff WHERE lower(staff_id) = lower(?)', (code,)).fetchone()
        except sqlite3.OperationalError:
            conn.close()
            flash('Sign up is not ready yet. Please tell the administrator.')
            return render_template('teacher_register.html')

        # One shared message, so nobody can use this form to find out which codes exist.
        if not rec or normalize_name(full_name) != normalize_name(rec['full_name']):
            conn.close()
            record_fail('sreg:' + client_ip())
            flash('We could not verify your details. Please check your full name and access code.')
            return render_template('teacher_register.html')

        if conn.execute('SELECT 1 FROM teachers WHERE staff_id = ?', (rec['staff_id'],)).fetchone():
            conn.close()
            flash('This access code was already used. Please log in with your full name and password.')
            return redirect(url_for('teacher_login'))

        # The username is made automatically from the name (juan.d.dela.cruz); people log in with their full name anyway.
        base = re.sub(r'[^a-z0-9]+', '.', normalize_name(rec['full_name'])).strip('.') or 'staff'
        try:
            for n in range(0, 50):
                username = base if n == 0 else f'{base}{n + 1}'
                if conn.execute('SELECT 1 FROM teachers WHERE lower(username) = lower(?)', (username,)).fetchone():
                    continue
                try:
                    cur = conn.execute('INSERT INTO teachers (username, password_hash, is_admin, role, full_name, staff_id) VALUES (?, ?, 0, ?, ?, ?)',
                                       (username, generate_password_hash(password), rec['role'], rec['full_name'], rec['staff_id']))
                    conn.commit()
                except sqlite3.IntegrityError:
                    continue
                session.clear()
                session.permanent = True
                session['teacher_id'] = cur.lastrowid
                session['t_username'] = rec['full_name']
                session['teacher_role'] = rec['role']
                session['staff_id'] = rec['staff_id']
                flash('Account created! From now on, log in with your full name and the password you just chose.')
                return redirect(url_for('teacher_dashboard'))
            flash('Could not create the account. Please tell the administrator.')
        finally:
            conn.close()
    return render_template('teacher_register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# --- STUDENT LMS ROUTES ---
@app.route('/dashboard')
def dashboard():
    if 'student_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    student_id = session['student_id']
    
    # SAFELY FETCH PROFILE DATA (Auto-creates columns if they are missing!)
    try:
        student = conn.execute('SELECT lrn, phone, profile_pic FROM students WHERE id = ?', (student_id,)).fetchone()
        current_user_lrn = student['lrn'] if student['lrn'] else ''
        current_user_phone = student['phone'] if student['phone'] else ''
        current_user_profile_pic = student['profile_pic']
    except sqlite3.OperationalError:
        conn.execute('ALTER TABLE students ADD COLUMN phone TEXT')
        conn.execute('ALTER TABLE students ADD COLUMN profile_pic TEXT')
        conn.commit()
        student = conn.execute('SELECT lrn, phone, profile_pic FROM students WHERE id = ?', (student_id,)).fetchone()
        current_user_lrn = student['lrn'] if student['lrn'] else ''
        current_user_phone = ''
        current_user_profile_pic = None
    
    subjects = conn.execute('''
        SELECT s.id, s.subject_name, s.code, sec.section_name, t.username as teacher 
        FROM subjects s 
        JOIN enrollments e ON s.id = e.subject_id 
        JOIN sections sec ON s.section_id = sec.id
        JOIN teachers t ON s.teacher_id = t.id 
        WHERE e.student_id = ?
    ''', (student_id,)).fetchall()
    
    # School-wide announcements (subject_id 0) + announcements of the classes this student joined
    announcements = conn.execute('''
        SELECT a.*, IFNULL(s.subject_name, 'School-Wide') AS subject_name
        FROM announcements a
        LEFT JOIN subjects s ON a.subject_id = s.id
        WHERE a.subject_id = 0 OR a.subject_id IS NULL
           OR a.subject_id IN (SELECT subject_id FROM enrollments WHERE student_id = ?)
        ORDER BY a.id DESC
    ''', (student_id,)).fetchall()
    
    events = conn.execute('''
        SELECT ev.*, IFNULL(s.subject_name, 'Campus-Wide Event') as subject_name
        FROM events ev
        LEFT JOIN subjects s ON ev.subject_id = s.id
        WHERE ev.is_school_wide = 1 OR ev.subject_id = 0 
        OR ev.subject_id IN (SELECT subject_id FROM enrollments WHERE student_id = ?)
        ORDER BY ev.event_date ASC
    ''', (student_id,)).fetchall()
    
    assignments = conn.execute('''
        SELECT a.*, s.subject_name 
        FROM assignments a 
        JOIN enrollments e ON a.subject_id = e.subject_id 
        JOIN subjects s ON a.subject_id = s.id 
        WHERE e.student_id = ? 
        ORDER BY a.due_date ASC
    ''', (student_id,)).fetchall()
    
    submissions = conn.execute('SELECT * FROM submissions WHERE student_id = ?', (student_id,)).fetchall()
    
    quizzes = conn.execute('''
        SELECT q.*, s.subject_name 
        FROM quizzes q 
        JOIN enrollments e ON q.subject_id = e.subject_id 
        JOIN subjects s ON q.subject_id = s.id 
        WHERE e.student_id = ? ORDER BY q.id DESC
    ''', (student_id,)).fetchall()
    
    attempts = conn.execute('SELECT * FROM quiz_attempts WHERE student_id = ?', (student_id,)).fetchall()

    # What this student already answered for each event (so the buttons show the right state)
    my_reactions = {r['event_id']: r['action'] for r in
                    conn.execute('SELECT event_id, action FROM event_reactions WHERE student_id = ?', (student_id,)).fetchall()}
    
    current_time = datetime.now().strftime('%Y-%m-%dT%H:%M')
    
    conn.close()
    return render_template('dashboard.html', 
                           full_name=session['full_name'],
                           current_user_lrn=current_user_lrn,
                           current_user_phone=current_user_phone,
                           current_user_profile_pic=current_user_profile_pic,
                           subjects=subjects, announcements=announcements, events=events, 
                           assignments=assignments, submissions=submissions, quizzes=quizzes, 
                           attempts=attempts, current_time=current_time,
                           my_reactions=my_reactions)

@app.route('/student/enroll', methods=['POST'])
def enroll_subject():
    if 'student_id' not in session: return redirect(url_for('login'))
    code = request.form['subject_code'].strip().upper()
    conn = get_db_connection()
    
    # 1. AUTO-FIX EXISTING DUPLICATES IN THE DATABASE
    # This deletes any duplicate enrollments, keeping only the first one they ever made
    conn.execute('''
        DELETE FROM enrollments 
        WHERE rowid NOT IN (
            SELECT MIN(rowid) 
            FROM enrollments 
            GROUP BY student_id, subject_id
        )
    ''')
    conn.commit()

    subject = conn.execute('SELECT id FROM subjects WHERE code = ?', (code,)).fetchone()
    
    if subject:
        # 2. CHECK IF THEY ARE ALREADY ENROLLED BEFORE INSERTING
        existing = conn.execute('SELECT * FROM enrollments WHERE student_id = ? AND subject_id = ?', 
                                (session['student_id'], subject['id'])).fetchone()
        
        if existing:
            flash('You are already enrolled in this class!')
        else:
            conn.execute('INSERT INTO enrollments (student_id, subject_id) VALUES (?, ?)', 
                         (session['student_id'], subject['id']))
            conn.commit()
            flash('Successfully enrolled in class!')
    else: 
        flash('Invalid subject code.')
        
    conn.close()
    return redirect(url_for('dashboard'))

@app.route('/update_profile', methods=['POST'])
def update_profile():
    if 'student_id' not in session: return redirect(url_for('login'))

    # NOTE: the LRN and full name can NOT be changed here. They are the student's verified identity.
    pic = request.files.get('profile_pic')
    if not (pic and pic.filename):
        flash('Please choose a picture first.')
        return redirect(url_for('dashboard'))

    filename = save_upload(pic, IMAGE_EXT)
    if not filename:
        flash('Profile picture must be a PNG, JPG, GIF or WEBP image.')
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    conn.execute('UPDATE students SET profile_pic=? WHERE id=?', (filename, session['student_id']))
    conn.commit()
    conn.close()

    flash("Profile updated successfully!")
    return redirect(url_for('dashboard'))

# --- TEACHER LMS ROUTES ---
@app.route('/teacher/dashboard')
def teacher_dashboard():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    conn = get_db_connection()
    t_id = session['teacher_id']
    
    # Safely ensure section_id exists in the students table
    try:
        conn.execute('SELECT section_id FROM students LIMIT 1')
    except sqlite3.OperationalError:
        conn.execute('ALTER TABLE students ADD COLUMN section_id INTEGER')
        conn.commit()
    
    # STUDENT LIST = every Grade 11 student on the masterlist (signed up or not yet),
    # plus any signed-up students who are not on the masterlist (e.g. added by the admin)
    try:
        listed = conn.execute('''
            SELECT a.lrn, a.full_name, a.section AS official_section,
                   st.id AS id, st.section_id, sec.section_name,
                   CASE WHEN st.id IS NULL THEN 0 ELSE 1 END AS signed_up
            FROM allowed_lrns a
            LEFT JOIN students st ON st.lrn = a.lrn
            LEFT JOIN sections sec ON st.section_id = sec.id
            WHERE a.grade_level = 11
        ''').fetchall()
        others = conn.execute('''
            SELECT st.lrn, st.full_name, NULL AS official_section,
                   st.id AS id, st.section_id, sec.section_name, 1 AS signed_up
            FROM students st
            LEFT JOIN sections sec ON st.section_id = sec.id
            WHERE st.lrn NOT IN (SELECT lrn FROM allowed_lrns WHERE grade_level = 11)
        ''').fetchall()
    except sqlite3.OperationalError:
        # masterlist not imported yet: show only the students who signed up
        listed = []
        others = conn.execute('''
            SELECT st.lrn, st.full_name, NULL AS official_section,
                   st.id AS id, st.section_id, sec.section_name, 1 AS signed_up
            FROM students st
            LEFT JOIN sections sec ON st.section_id = sec.id
        ''').fetchall()

    all_students = sorted([dict(r) for r in list(listed) + list(others)],
                          key=lambda r: (r['full_name'] or '').lower())
    signup_stats = {'signed_up': sum(1 for r in all_students if r['signed_up']),
                    'total': len(all_students)}

    # SSLG accounts never receive the student list or LRNs
    role = session.get('teacher_role', 'teacher')
    if role == 'sslg':
        all_students = []
        signup_stats = {'signed_up': 0, 'total': 0}

    sections = conn.execute('SELECT * FROM sections WHERE teacher_id = ? ORDER BY year_level, section_name', (t_id,)).fetchall()
    subjects = conn.execute('SELECT * FROM subjects WHERE teacher_id = ?', (t_id,)).fetchall()
    # Teachers, SSLG and the admin all see the SAME announcements and events (whoever posted them)
    announcements = conn.execute('''
        SELECT a.*, IFNULL(s.subject_name, 'School-Wide') AS subject_name,
               IFNULL(NULLIF(t.full_name, ''), IFNULL(t.username, 'Unknown')) AS poster
        FROM announcements a
        LEFT JOIN subjects s ON a.subject_id = s.id
        LEFT JOIN teachers t ON a.teacher_id = t.id
        ORDER BY a.id DESC
    ''').fetchall()
    events = conn.execute('''
        SELECT ev.*, IFNULL(s.subject_name, 'School-Wide Event') as subject_name,
               IFNULL(NULLIF(t.full_name, ''), IFNULL(t.username, 'Unknown')) AS poster
        FROM events ev
        LEFT JOIN subjects s ON ev.subject_id = s.id
        LEFT JOIN teachers t ON ev.teacher_id = t.id
        ORDER BY ev.event_date ASC
    ''').fetchall()
    me = conn.execute('SELECT is_admin FROM teachers WHERE id = ?', (t_id,)).fetchone()
    is_admin = bool(me and me['is_admin'] == 1)
    # WHO answered each of this teacher's events: {event_id: {'going': [...], 'interested': [...], 'not_going': [...]}}
    responses = {}
    for r in conn.execute('''
        SELECT er.event_id, er.action, st.full_name, st.lrn
        FROM event_reactions er
        JOIN events ev ON ev.id = er.event_id
        JOIN students st ON st.id = er.student_id
        ORDER BY st.full_name
    ''').fetchall():
        bucket = responses.setdefault(r['event_id'], {'going': [], 'interested': [], 'not_going': []})
        bucket.setdefault(r['action'], []).append({'name': r['full_name'], 'lrn': None if role == 'sslg' else r['lrn']})

    assignments = conn.execute('SELECT a.*, s.subject_name, s.code FROM assignments a JOIN subjects s ON a.subject_id = s.id WHERE a.teacher_id = ? ORDER BY a.date_posted DESC', (t_id,)).fetchall()
    quizzes = conn.execute('SELECT q.*, s.subject_name FROM quizzes q JOIN subjects s ON q.subject_id = s.id WHERE q.teacher_id = ? ORDER BY q.date_posted DESC', (t_id,)).fetchall()
    
    conn.close()
    
    # Notice we added students=all_students to the template renderer!
    return render_template('teacher_dashboard.html', username=session['t_username'], role=role, 
                           signup_stats=signup_stats, students=all_students, sections=sections, 
                           subjects=subjects, announcements=announcements, events=events, 
                           assignments=assignments, quizzes=quizzes,
                           responses=responses, is_admin=is_admin, my_id=t_id)

@app.route('/assign_section', methods=['POST'])
def assign_section():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    
    student_id = request.form.get('student_id')
    section_id = request.form.get('section_id')
    
    conn = get_db_connection()
    conn.execute('UPDATE students SET section_id = ? WHERE id = ?', (section_id, student_id))
    conn.commit()
    conn.close()
    
    flash('Student assigned to section successfully!')
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/section/add', methods=['POST'])
def add_section():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    conn = get_db_connection()
    conn.execute('INSERT INTO sections (teacher_id, section_name, year_level) VALUES (?, ?, ?)', 
                 (session['teacher_id'], request.form['section_name'], request.form['year_level']))
    conn.commit()
    conn.close()
    flash(f"Section '{request.form['section_name']}' created!")
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/subject/add', methods=['POST'])
def add_subject():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    name = request.form['subject_name']
    section_id = request.form['section_id']
    code = generate_subject_code()
    conn = get_db_connection()
    conn.execute('INSERT INTO subjects (section_id, teacher_id, subject_name, code) VALUES (?, ?, ?, ?)', 
                 (section_id, session['teacher_id'], name, code))
    conn.commit()
    conn.close()
    flash(f'Subject created! Share code: {code}')
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/announcement/add', methods=['POST'])
def add_announcement():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    subject_id = request.form['subject_id']
    if session.get('teacher_role') == 'sslg':
        subject_id = '0'                                   # SSLG can only post school-wide
    conn = get_db_connection()
    if subject_id != '0' and not teacher_owns_subject(conn, subject_id):
        conn.close()
        flash('You can only post to your own classes.')
        return redirect(url_for('teacher_dashboard'))
    conn.execute('INSERT INTO announcements (subject_id, teacher_id, title, content) VALUES (?, ?, ?, ?)',
                 (subject_id, session['teacher_id'], request.form['title'], request.form['content']))
    conn.commit()
    conn.close()
    flash('Announcement posted!')
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/event/add', methods=['POST'])
def add_event():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    
    title = request.form['title']
    event_date = request.form['event_date']
    description = request.form['description']
    subject_id = request.form.get('subject_id', 0)
    form_link = request.form.get('form_link', '')
    
    # Capture the custom category from the teacher form (defaults to 'General Events')
    category = request.form.get('category', 'General Events').strip()
    if not category:
        category = 'General Events'
        
    is_school_wide = 1 if request.form.get('is_school_wide') == 'on' else 0
    if session.get('teacher_role') == 'sslg':
        subject_id, is_school_wide = 0, 1                  # SSLG events are always school-wide
    
    # Handle optional event image upload
    image_path = None
    img = request.files.get('image')
    if img and img.filename:
        image_path = save_upload(img, IMAGE_EXT)
        if not image_path:
            flash('Event image must be a PNG, JPG, GIF or WEBP image.')
            return redirect(url_for('teacher_dashboard'))
            
    conn = get_db_connection()
    
    # Make sure your 'events' table has a 'category' column. 
    # If it doesn't exist yet, you can run: ALTER TABLE events ADD COLUMN category TEXT DEFAULT 'General Events';
    conn.execute('''INSERT INTO events 
                    (subject_id, teacher_id, title, event_date, description, image_path, form_link, is_school_wide, category) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                 (subject_id, session['teacher_id'], title, event_date, description, image_path, form_link, is_school_wide, category))
    conn.commit()
    conn.close()
    
    flash('Event and gallery category published successfully!')
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/approve/<int:student_id>', methods=['POST'])
def approve_student(student_id):
    denied = admin_required()
    if denied: return denied
    conn = get_db_connection()
    conn.execute('UPDATE students SET is_approved = 1 WHERE id = ?', (student_id,))
    conn.commit()
    conn.close()
    flash('Student approved successfully!')
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/subject/delete/<int:subject_id>', methods=['POST'])
def delete_subject(subject_id):
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    
    conn = get_db_connection()
    # Verify the subject actually belongs to this teacher for security
    subject = conn.execute('SELECT id FROM subjects WHERE id = ? AND teacher_id = ?', 
                           (subject_id, session['teacher_id'])).fetchone()
    
    if subject:
        # Delete all related data first so your database doesn't crash from orphaned files
        conn.execute('DELETE FROM enrollments WHERE subject_id = ?', (subject_id,))
        conn.execute('DELETE FROM announcements WHERE subject_id = ?', (subject_id,))
        conn.execute('DELETE FROM events WHERE subject_id = ?', (subject_id,))
        conn.execute('DELETE FROM assignments WHERE subject_id = ?', (subject_id,))
        conn.execute('DELETE FROM quizzes WHERE subject_id = ?', (subject_id,))
        
        # Finally, delete the actual subject
        conn.execute('DELETE FROM subjects WHERE id = ?', (subject_id,))
        conn.commit()
        flash('Subject and all related data deleted successfully!')
    else:
        flash('Unauthorized or subject not found.')
        
    conn.close()
    return redirect(url_for('teacher_dashboard'))

# --- CLASSROOM FEATURES (WITH FILE UPLOADS) ---
@app.route('/add_assignment', methods=['POST'])
def add_assignment():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    
    subject_id = request.form['subject_id']
    title = request.form['title']
    instructions = request.form['instructions']
    due_date = request.form['due_date']
    t_id = session['teacher_id']

    conn = get_db_connection()
    if not teacher_owns_subject(conn, subject_id):
        conn.close()
        flash('You can only post to your own classes.')
        return redirect(url_for('teacher_dashboard'))

    file_path = None
    up = request.files.get('file')
    if up and up.filename:
        file_path = save_upload(up, DOC_EXT)
        if not file_path:
            conn.close()
            flash('That file type is not allowed.')
            return redirect(url_for('teacher_dashboard'))

    conn.execute('INSERT INTO assignments (subject_id, teacher_id, title, instructions, due_date, file_path) VALUES (?, ?, ?, ?, ?, ?)',
                 (subject_id, t_id, title, instructions, due_date, file_path))
    conn.commit()
    conn.close()
    
    flash("Assignment posted successfully!")
    return redirect(url_for('teacher_dashboard'))

@app.route('/submit_assignment', methods=['POST'])
def submit_assignment():
    if 'student_id' not in session: return redirect(url_for('login'))
        
    assignment_id = request.form['assignment_id']
    answer_text = request.form.get('answer_text', '')
    s_id = session['student_id']
    
    conn = get_db_connection()
    allowed = conn.execute('''SELECT 1 FROM assignments a JOIN enrollments e ON e.subject_id = a.subject_id
                              WHERE a.id = ? AND e.student_id = ?''', (assignment_id, s_id)).fetchone()
    if not allowed:
        conn.close()
        flash('You are not enrolled in this class.')
        return redirect(url_for('dashboard'))

    file_path = None
    up = request.files.get('file')
    if up and up.filename:
        file_path = save_upload(up, DOC_EXT)
        if not file_path:
            conn.close()
            flash('That file type is not allowed.')
            return redirect(url_for('dashboard'))

    existing = conn.execute('SELECT * FROM submissions WHERE assignment_id = ? AND student_id = ?', (assignment_id, s_id)).fetchone()
    
    if existing:
        # Keep old file if a new one isn't uploaded
        final_file = file_path if file_path else existing['file_path']
        conn.execute('UPDATE submissions SET answer_text = ?, file_path = ?, date_submitted = CURRENT_TIMESTAMP WHERE id = ?', 
                     (answer_text, final_file, existing['id']))
        flash("Submission updated!")
    else:
        conn.execute('INSERT INTO submissions (assignment_id, student_id, answer_text, file_path) VALUES (?, ?, ?, ?)', 
                     (assignment_id, s_id, answer_text, file_path))
        flash("Assignment submitted successfully!")
        
    conn.commit()
    conn.close()
    return redirect(url_for('dashboard'))

# --- PHASE 2 & 3: SECURE TIMED EXAM ENGINE, ESSAYS & AUTO-GRADER ---

def calculate_quiz_score(attempt_id):
    conn = get_db_connection()
    attempt = conn.execute('SELECT * FROM quiz_attempts WHERE id = ?', (attempt_id,)).fetchone()
    if not attempt:
        conn.close()
        return
        
    questions = conn.execute('SELECT * FROM questions WHERE quiz_id = ?', (attempt['quiz_id'],)).fetchall()
    
    score = 0
    for q in questions:
        ans = conn.execute('SELECT * FROM student_answers WHERE attempt_id = ? AND question_id = ?', (attempt_id, q['id'])).fetchone()
        if ans:
            if q['question_type'] == 'MCQ':
                if ans['selected_option'] == q['correct_option']:
                    score += q['points']
            elif q['question_type'] == 'Essay':
                if ans['is_graded'] == 1:
                    score += ans['points_awarded']
            
    conn.execute('UPDATE quiz_attempts SET score = ? WHERE id = ?', (score, attempt_id))
    conn.commit()
    conn.close()

@app.route('/teacher/add_questions_bulk', methods=['POST'])
def add_questions_bulk():
    if 'teacher_id' not in session: return jsonify({'status': 'error', 'message': 'Unauthorized'})
    
    data = request.get_json()
    quiz_id = data.get('quiz_id')
    questionsList = data.get('questions')
    
    conn = get_db_connection()
    try:
        if not teacher_owns_quiz(conn, quiz_id):
            return jsonify({'status': 'error', 'message': 'You can only edit your own exams.'}), 403
        count = conn.execute('SELECT COUNT(*) FROM questions WHERE quiz_id = ?', (quiz_id,)).fetchone()[0]
        page_num = count + 1
        
        for q in questionsList:
            q_type = q.get('type', 'MCQ')
            q_text = q.get('text', '')
            points = int(q.get('points', 1))
            
            if q_type == 'MCQ':
                conn.execute('''INSERT INTO questions (quiz_id, question_type, question_text, option_a, option_b, option_c, option_d, correct_option, points, page_number) 
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                             (quiz_id, 'MCQ', q_text, q.get('option_a'), q.get('option_b'), q.get('option_c'), q.get('option_d'), q.get('correct_option'), points, page_num))
            elif q_type == 'Essay':
                # FIXED: Provide placeholder text for MCQ fields to bypass legacy database constraints
                conn.execute('''INSERT INTO questions (quiz_id, question_type, question_text, option_a, option_b, option_c, option_d, correct_option, points, page_number) 
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                             (quiz_id, 'Essay', q_text, 'N/A', 'N/A', 'N/A', 'N/A', 'N/A', points, page_num))
            page_num += 1
            
        conn.commit()
        return jsonify({'status': 'success', 'message': f'{len(questionsList)} questions added!'})
        
    except Exception as e:
        # If an error happens, return it to the frontend without crashing the server
        return jsonify({'status': 'error', 'message': str(e)}), 400
        
    finally:
        # FIXED: This guarantees the database connection always closes, preventing "database is locked" errors
        conn.close()

@app.route('/teacher/ungraded_essays')
def ungraded_essays():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    t_id = session['teacher_id']
    conn = get_db_connection()
    
    # Creates a master list of all ungraded essays for this teacher across all exams
    ungraded = conn.execute('''
        SELECT sa.id as answer_id, sa.selected_option as essay_text, qa.id as attempt_id, 
               q.question_text, q.points as max_points, s.full_name, s.lrn, qu.title as quiz_title, qu.id as quiz_id
        FROM student_answers sa
        JOIN questions q ON sa.question_id = q.id
        JOIN quiz_attempts qa ON sa.attempt_id = qa.id
        JOIN students s ON qa.student_id = s.id
        JOIN quizzes qu ON q.quiz_id = qu.id
        WHERE q.question_type = 'Essay' AND sa.is_graded = 0 AND qu.teacher_id = ? AND qa.is_completed = 1
    ''', (t_id,)).fetchall()
    
    conn.close()
    return render_template('ungraded_essays.html', ungraded=ungraded)

@app.route('/teacher/grade_essay', methods=['POST'])
def grade_essay():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))

    try:
        answer_id = int(request.form['answer_id'])
        points_awarded = int(request.form['points_awarded'])
    except (KeyError, ValueError):
        flash('Invalid score.')
        return redirect(url_for('ungraded_essays'))

    conn = get_db_connection()
    # The essay must belong to one of THIS teacher's exams
    row = conn.execute('''
        SELECT sa.attempt_id, q.points AS max_points
        FROM student_answers sa
        JOIN questions q ON sa.question_id = q.id
        JOIN quizzes qu ON q.quiz_id = qu.id
        WHERE sa.id = ? AND qu.teacher_id = ?
    ''', (answer_id, session['teacher_id'])).fetchone()
    if not row or not (0 <= points_awarded <= row['max_points']):
        conn.close()
        flash('That score is not allowed for this essay.')
        return redirect(url_for('ungraded_essays'))

    conn.execute('UPDATE student_answers SET points_awarded = ?, is_graded = 1 WHERE id = ?', (points_awarded, answer_id))
    conn.commit()
    conn.close()

    # Automatically recalculates the student's total test score once graded
    calculate_quiz_score(row['attempt_id'])

    flash('Essay graded successfully! Student score updated.')
    return redirect(url_for('ungraded_essays'))

@app.route('/teacher/create_quiz', methods=['POST'])
def create_quiz():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    conn = get_db_connection()
    if not teacher_owns_subject(conn, request.form['subject_id']):
        conn.close()
        flash('You can only create exams for your own classes.')
        return redirect(url_for('teacher_dashboard'))
    conn.execute('INSERT INTO quizzes (subject_id, teacher_id, title, duration_minutes, due_date) VALUES (?, ?, ?, ?, ?)',
                 (request.form['subject_id'], session['teacher_id'], request.form['title'], request.form['duration_minutes'], request.form['due_date']))
    conn.commit()
    conn.close()
    flash('Quiz created! You can now add questions to it.')
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/add_question', methods=['POST'])
def add_question():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    conn = get_db_connection()
    
    quiz_id = request.form['quiz_id']
    if not teacher_owns_quiz(conn, quiz_id):
        conn.close()
        flash('You can only edit your own exams.')
        return redirect(url_for('teacher_dashboard'))
    count = conn.execute('SELECT COUNT(*) FROM questions WHERE quiz_id = ?', (quiz_id,)).fetchone()[0]
    page_num = count + 1
    
    conn.execute('''INSERT INTO questions (quiz_id, question_text, option_a, option_b, option_c, option_d, correct_option, page_number) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                 (quiz_id, request.form['question_text'], request.form['option_a'], request.form['option_b'], 
                  request.form['option_c'], request.form['option_d'], request.form['correct_option'], page_num))
    conn.commit()
    conn.close()
    flash(f'Question added to Page {page_num}!')
    return redirect(url_for('teacher_dashboard'))

# --- NEW: EXAM MANAGEMENT ROUTES ---
@app.route('/teacher/manage_quiz/<int:quiz_id>')
def manage_quiz(quiz_id):
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    conn = get_db_connection()
    if not teacher_owns_quiz(conn, quiz_id):
        conn.close()
        flash('You can only view your own exams.')
        return redirect(url_for('teacher_dashboard'))
    
    quiz = conn.execute('SELECT q.*, s.subject_name FROM quizzes q JOIN subjects s ON q.subject_id = s.id WHERE q.id = ?', (quiz_id,)).fetchone()
    questions = conn.execute('SELECT * FROM questions WHERE quiz_id = ? ORDER BY page_number ASC', (quiz_id,)).fetchall()
    
    attempts = conn.execute('''
        SELECT qa.*, s.full_name, s.lrn 
        FROM quiz_attempts qa 
        JOIN students s ON qa.student_id = s.id 
        WHERE qa.quiz_id = ? AND qa.is_completed = 1
        ORDER BY qa.score DESC
    ''', (quiz_id,)).fetchall()
    
    conn.close()
    return render_template('manage_quiz.html', quiz=quiz, questions=questions, attempts=attempts, max_score=len(questions))

@app.route('/teacher/edit_question', methods=['POST'])
def edit_question():
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    q_id = request.form['question_id']
    new_correct = request.form['correct_option']
    quiz_id = request.form['quiz_id']
    
    conn = get_db_connection()
    if not teacher_owns_quiz(conn, quiz_id):
        conn.close()
        flash('You can only edit your own exams.')
        return redirect(url_for('teacher_dashboard'))
    conn.execute('UPDATE questions SET correct_option = ? WHERE id = ? AND quiz_id = ?', (new_correct, q_id, quiz_id))
    
    # Re-grade all completed attempts automatically!
    attempts = conn.execute('SELECT id FROM quiz_attempts WHERE quiz_id = ? AND is_completed = 1', (quiz_id,)).fetchall()
    conn.commit()
    conn.close()
    
    for att in attempts:
        calculate_quiz_score(att['id'])
        
    flash('Answer updated! All student scores have been automatically recalculated.')
    return redirect(url_for('manage_quiz', quiz_id=quiz_id))

# --- STUDENT EXAM ROUTES ---
@app.route('/student/quiz/start/<int:quiz_id>')
def start_quiz(quiz_id):
    if 'student_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    s_id = session['student_id']

    quiz_row = conn.execute('SELECT subject_id FROM quizzes WHERE id = ?', (quiz_id,)).fetchone()
    enrolled = quiz_row and conn.execute('SELECT 1 FROM enrollments WHERE student_id = ? AND subject_id = ?',
                                         (s_id, quiz_row['subject_id'])).fetchone()
    if not enrolled:
        conn.close()
        flash('You are not enrolled in this class.')
        return redirect(url_for('dashboard'))
    
    attempt = conn.execute('SELECT * FROM quiz_attempts WHERE quiz_id = ? AND student_id = ?', (quiz_id, s_id)).fetchone()
    
    if not attempt:
        start_time = datetime.now().isoformat()
        cursor = conn.execute('INSERT INTO quiz_attempts (quiz_id, student_id, start_time, highest_page_reached) VALUES (?, ?, ?, 1)', 
                              (quiz_id, s_id, start_time))
        attempt_id = cursor.lastrowid
        conn.commit()
    else:
        attempt_id = attempt['id']
        if attempt['is_completed'] == 1:
            conn.close()
            flash('You have already completed this exam.')
            return redirect(url_for('dashboard'))
            
    conn.close()
    return redirect(url_for('quiz_page', attempt_id=attempt_id, page_num=1))

@app.route('/student/quiz/<int:attempt_id>/page/<int:page_num>')
def quiz_page(attempt_id, page_num):
    if 'student_id' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    
    attempt = conn.execute('SELECT * FROM quiz_attempts WHERE id = ?', (attempt_id,)).fetchone()
    if not attempt or attempt['student_id'] != session['student_id']:
        conn.close()
        flash('Exam not found.')
        return redirect(url_for('dashboard'))
    if attempt['is_completed'] == 1:
        conn.close()
        flash('You have already completed this exam.')
        return redirect(url_for('dashboard'))
    quiz = conn.execute('SELECT * FROM quizzes WHERE id = ?', (attempt['quiz_id'],)).fetchone()
    
    start_time = datetime.fromisoformat(attempt['start_time'])
    elapsed_minutes = (datetime.now() - start_time).total_seconds() / 60.0
    
    if elapsed_minutes >= quiz['duration_minutes']:
        conn.execute('UPDATE quiz_attempts SET is_completed = 1 WHERE id = ?', (attempt_id,))
        conn.commit()
        conn.close()
        calculate_quiz_score(attempt_id) # Calculate grade on timeout
        flash('Time is up! Your exam was automatically submitted.')
        return redirect(url_for('dashboard'))
        
    highest = attempt['highest_page_reached']
    if page_num < highest:
        flash("You cannot go back to previous questions!")
        return redirect(url_for('quiz_page', attempt_id=attempt_id, page_num=highest))
    
    if page_num > highest:
        conn.execute('UPDATE quiz_attempts SET highest_page_reached = ? WHERE id = ?', (page_num, attempt_id))
        conn.commit()
        
    question = conn.execute('SELECT * FROM questions WHERE quiz_id = ? AND page_number = ?', (quiz['id'], page_num)).fetchone()
    
    saved_ans = ""
    if question:
        ans = conn.execute('SELECT selected_option FROM student_answers WHERE attempt_id = ? AND question_id = ?', (attempt_id, question['id'])).fetchone()
        if ans: saved_ans = ans['selected_option']
        
    conn.close()
    
    if not question:
        conn = get_db_connection()
        conn.execute('UPDATE quiz_attempts SET is_completed = 1 WHERE id = ?', (attempt_id,))
        conn.commit()
        conn.close()
        calculate_quiz_score(attempt_id) # Calculate grade on completion
        flash('Exam completed and submitted successfully!')
        return redirect(url_for('dashboard'))
        
    remaining_time_seconds = int((quiz['duration_minutes'] * 60) - (elapsed_minutes * 60))
    return render_template('quiz_page.html', question=question, attempt_id=attempt_id, remaining_time=remaining_time_seconds, saved_ans=saved_ans)

@app.route('/student/quiz/autosave', methods=['POST'])
def autosave_quiz():
    if 'student_id' not in session:
        return jsonify({"status": "error", "message": "Please log in."}), 401

    data = request.get_json(silent=True) or {}
    try:
        attempt_id = int(data['attempt_id'])
        question_id = int(data['question_id'])
    except (KeyError, ValueError, TypeError):
        return jsonify({"status": "error"}), 400
    selected = str(data.get('selected_option', ''))[:5000]

    conn = get_db_connection()
    # It must be THIS student's own attempt, still in progress and not out of time
    attempt = conn.execute('SELECT * FROM quiz_attempts WHERE id = ? AND student_id = ?',
                           (attempt_id, session['student_id'])).fetchone()
    if not attempt or attempt['is_completed'] == 1:
        conn.close()
        return jsonify({"status": "error", "message": "This exam is closed."}), 403
    quiz = conn.execute('SELECT duration_minutes FROM quizzes WHERE id = ?', (attempt['quiz_id'],)).fetchone()
    elapsed = (datetime.now() - datetime.fromisoformat(attempt['start_time'])).total_seconds() / 60.0
    if elapsed >= quiz['duration_minutes']:
        conn.close()
        return jsonify({"status": "error", "message": "Time is up."}), 403

    # The question must belong to this exam
    question = conn.execute('SELECT question_type FROM questions WHERE id = ? AND quiz_id = ?',
                            (question_id, attempt['quiz_id'])).fetchone()
    if not question:
        conn.close()
        return jsonify({"status": "error"}), 400

    if question['question_type'] == 'Essay':
        is_graded = 0                      # essays always wait for the teacher
    else:
        if selected not in ('A', 'B', 'C', 'D'):
            conn.close()
            return jsonify({"status": "error"}), 400
        is_graded = 1

    existing = conn.execute('SELECT id FROM student_answers WHERE attempt_id = ? AND question_id = ?', (attempt_id, question_id)).fetchone()
    if existing:
        conn.execute('UPDATE student_answers SET selected_option = ?, is_graded = ? WHERE id = ?', (selected, is_graded, existing['id']))
    else:
        conn.execute('INSERT INTO student_answers (attempt_id, question_id, selected_option, is_graded) VALUES (?, ?, ?, ?)', (attempt_id, question_id, selected, is_graded))

    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

# ---ADMIN DASHBOARD ---
@app.route('/admin')
def admin_dashboard():
    denied = admin_required()
    if denied: return denied

    conn = get_db_connection()
    teachers = conn.execute('SELECT * FROM teachers WHERE is_admin = 0').fetchall()
    students = conn.execute('SELECT * FROM students').fetchall()
    conn.close()
    return render_template('admin_dashboard.html', teachers=teachers, students=students)

@app.route('/admin/add_user', methods=['POST'])
def admin_add_user():
    denied = admin_required()
    if denied: return denied

    role = request.form.get('role', '')
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')

    if role not in ('teacher', 'student') or not username or len(password) < 8:
        flash('Error: choose a role, a username, and a password of at least 8 characters.')
        return redirect(url_for('admin_dashboard'))
    password_hash = generate_password_hash(password)

    conn = get_db_connection()
    try:
        if role == 'teacher':
            conn.execute('INSERT INTO teachers (username, password_hash) VALUES (?, ?)', (username, password_hash))
        else:
            lrn = request.form.get('lrn', '').strip()
            full_name = request.form.get('full_name', '').strip()
            if not (lrn.isascii() and lrn.isdigit() and len(lrn) == 12) or not full_name:
                flash('Error: a student needs a full name and a 12-digit LRN.')
                return redirect(url_for('admin_dashboard'))
            conn.execute('INSERT INTO students (username, password_hash, full_name, lrn, is_approved) VALUES (?, ?, ?, ?, 1)',
                         (username, password_hash, full_name, lrn))
        conn.commit()
        flash(f'Success: {role.capitalize()} {username} added!')
    except sqlite3.IntegrityError:
        flash('Error: Username or LRN already exists.')
    finally:
        conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_user/<role>/<int:user_id>', methods=['POST'])
def admin_delete_user(role, user_id):
    denied = admin_required()
    if denied: return denied

    conn = get_db_connection()
    if role == 'teacher':
        conn.execute('DELETE FROM teachers WHERE id = ? AND is_admin = 0', (user_id,))   # the admin can never be deleted
    elif role == 'student':
        # remove the student's exam answers, attempts, submissions and enrollments too
        conn.execute('DELETE FROM student_answers WHERE attempt_id IN (SELECT id FROM quiz_attempts WHERE student_id = ?)', (user_id,))
        conn.execute('DELETE FROM quiz_attempts WHERE student_id = ?', (user_id,))
        conn.execute('DELETE FROM submissions WHERE student_id = ?', (user_id,))
        conn.execute('DELETE FROM enrollments WHERE student_id = ?', (user_id,))
        conn.execute('DELETE FROM event_reactions WHERE student_id = ?', (user_id,))
        conn.execute('DELETE FROM students WHERE id = ?', (user_id,))
    else:
        conn.close()
        return "Not found", 404
    conn.commit()
    conn.close()
    flash(f'{role.capitalize()} account deleted successfully.')
    return redirect(url_for('admin_dashboard'))

# --- NEW EVENT FEATURES ---
@app.route('/event/react', methods=['POST'])
def react_event():
    if 'student_id' not in session: return jsonify({'status': 'error'}), 401

    data = request.get_json(silent=True) or {}
    action = data.get('action')   # 'interested', 'going' or 'not_going' (shown as "Not going / Maybe")
    try:
        event_id = int(data.get('event_id'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error'}), 400
    if action not in ('interested', 'going', 'not_going'):
        return jsonify({'status': 'error'}), 400

    COUNTERS = {'interested': 'interested_count', 'going': 'going_count'}   # not_going has no public counter
    sid = session['student_id']

    conn = get_db_connection()
    if not conn.execute('SELECT 1 FROM events WHERE id = ?', (event_id,)).fetchone():
        conn.close()
        return jsonify({'status': 'error'}), 404

    def drop(act):
        conn.execute('DELETE FROM event_reactions WHERE student_id = ? AND event_id = ? AND action = ?', (sid, event_id, act))
        if act in COUNTERS:
            col = COUNTERS[act]
            conn.execute(f'UPDATE events SET {col} = MAX({col} - 1, 0) WHERE id = ?', (event_id,))

    current = [r['action'] for r in conn.execute(
        'SELECT action FROM event_reactions WHERE student_id = ? AND event_id = ?', (sid, event_id)).fetchall()]

    if action in current:
        # Clicking the same button again = UNDO
        drop(action)
        my_reaction = None
    else:
        # One answer per student per event: choosing a new one replaces the old one
        for old in current:
            drop(old)
        conn.execute('INSERT OR IGNORE INTO event_reactions (student_id, event_id, action) VALUES (?, ?, ?)', (sid, event_id, action))
        if action in COUNTERS:
            conn.execute(f'UPDATE events SET {COUNTERS[action]} = {COUNTERS[action]} + 1 WHERE id = ?', (event_id,))
        my_reaction = action
    conn.commit()

    updated_event = conn.execute('SELECT interested_count, going_count FROM events WHERE id = ?', (event_id,)).fetchone()
    conn.close()

    return jsonify({
        'status': 'success',
        'interested': updated_event['interested_count'],
        'going': updated_event['going_count'],
        'my_reaction': my_reaction
    })

@app.route('/teacher/event/delete/<int:event_id>', methods=['POST'])
def delete_event(event_id):
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    
    conn = get_db_connection()
    # Everyone SEES every event, but only the person who posted it (or the admin) can delete it
    owner_clause = '' if is_admin_session(conn) else ' AND teacher_id = %d' % int(session['teacher_id'])
    conn.execute('DELETE FROM event_reactions WHERE event_id IN (SELECT id FROM events WHERE id = ?' + owner_clause + ')', (event_id,))
    conn.execute('DELETE FROM events WHERE id = ?' + owner_clause, (event_id,))
    conn.commit()
    conn.close()
    
    flash('Event deleted successfully.')
    return redirect(url_for('teacher_dashboard'))

@app.route('/teacher/announcement/delete/<int:ann_id>', methods=['POST'])
def delete_announcement(ann_id):
    if 'teacher_id' not in session: return redirect(url_for('teacher_login'))
    conn = get_db_connection()
    owner_clause = '' if is_admin_session(conn) else ' AND teacher_id = %d' % int(session['teacher_id'])
    conn.execute('DELETE FROM announcements WHERE id = ?' + owner_clause, (ann_id,))
    conn.commit()
    conn.close()
    flash('Announcement deleted.')
    return redirect(url_for('teacher_dashboard'))

# --- NOTIFICATIONS: the browser asks this every ~30 seconds and shows a badge / pop-up for new posts ---
@app.route('/api/latest')
def api_latest():
    conn = get_db_connection()
    try:
        if 'teacher_id' in session:
            me = int(session['teacher_id'])
            ann = conn.execute('SELECT a.id, a.title, IFNULL(NULLIF(t.full_name, ""), t.username) AS who FROM announcements a LEFT JOIN teachers t ON a.teacher_id = t.id WHERE IFNULL(a.teacher_id, 0) != ? ORDER BY a.id DESC LIMIT 20', (me,)).fetchall()
            evs = conn.execute('SELECT e.id, e.title, IFNULL(NULLIF(t.full_name, ""), t.username) AS who FROM events e LEFT JOIN teachers t ON e.teacher_id = t.id WHERE IFNULL(e.teacher_id, 0) != ? ORDER BY e.id DESC LIMIT 20', (me,)).fetchall()
            kind = 'staff'
        elif 'student_id' in session:
            sid = session['student_id']
            ann = conn.execute('''SELECT a.id, a.title, '' AS who FROM announcements a
                WHERE a.subject_id = 0 OR a.subject_id IS NULL OR a.subject_id IN (SELECT subject_id FROM enrollments WHERE student_id = ?)
                ORDER BY a.id DESC LIMIT 20''', (sid,)).fetchall()
            evs = conn.execute('''SELECT e.id, e.title, '' AS who FROM events e
                WHERE e.is_school_wide = 1 OR e.subject_id = 0 OR e.subject_id IN (SELECT subject_id FROM enrollments WHERE student_id = ?)
                ORDER BY e.id DESC LIMIT 20''', (sid,)).fetchall()
            kind = 'student'
        else:
            return jsonify({'kind': None, 'announcements': [], 'events': []})
        return jsonify({'kind': kind,
                        'announcements': [dict(r) for r in ann],
                        'events': [dict(r) for r in evs]})
    finally:
        conn.close()

if __name__ == '__main__':
    app.run(debug=False, port=5000)
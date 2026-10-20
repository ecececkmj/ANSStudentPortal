import sqlite3
import os
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()

def setup_database():
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        lrn TEXT UNIQUE NOT NULL, 
        username TEXT UNIQUE NOT NULL, 
        password_hash TEXT NOT NULL, 
        full_name TEXT NOT NULL, 
        is_approved INTEGER DEFAULT 0,
        phone TEXT,
        profile_pic TEXT,
        section_id INTEGER,
        email TEXT
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS teachers (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS sections (id INTEGER PRIMARY KEY AUTOINCREMENT, teacher_id INTEGER, section_name TEXT NOT NULL, year_level TEXT, FOREIGN KEY(teacher_id) REFERENCES teachers(id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS subjects (id INTEGER PRIMARY KEY AUTOINCREMENT, section_id INTEGER, teacher_id INTEGER, subject_name TEXT NOT NULL, code TEXT UNIQUE NOT NULL, FOREIGN KEY(section_id) REFERENCES sections(id), FOREIGN KEY(teacher_id) REFERENCES teachers(id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS enrollments (id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER, subject_id INTEGER, FOREIGN KEY(student_id) REFERENCES students(id), FOREIGN KEY(subject_id) REFERENCES subjects(id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS announcements (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, teacher_id INTEGER, title TEXT, content TEXT, date_posted TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(subject_id) REFERENCES subjects(id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, teacher_id INTEGER, title TEXT, event_date TEXT, description TEXT, FOREIGN KEY(subject_id) REFERENCES subjects(id))''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS assignments (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_id INTEGER, teacher_id INTEGER, title TEXT NOT NULL, instructions TEXT, due_date TEXT, file_path TEXT, date_posted TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(subject_id) REFERENCES subjects(id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS submissions (id INTEGER PRIMARY KEY AUTOINCREMENT, assignment_id INTEGER, student_id INTEGER, answer_text TEXT, file_path TEXT, grade TEXT DEFAULT 'Pending', date_submitted TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(assignment_id) REFERENCES assignments(id), FOREIGN KEY(student_id) REFERENCES students(id))''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS quizzes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        subject_id INTEGER, 
        teacher_id INTEGER, 
        title TEXT NOT NULL, 
        duration_minutes INTEGER NOT NULL, 
        date_posted TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
        FOREIGN KEY(subject_id) REFERENCES subjects(id)
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        quiz_id INTEGER, 
        question_type TEXT DEFAULT 'MCQ', 
        question_text TEXT NOT NULL, 
        option_a TEXT, 
        option_b TEXT, 
        option_c TEXT, 
        option_d TEXT, 
        correct_option TEXT, 
        points INTEGER DEFAULT 1,
        page_number INTEGER NOT NULL, 
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id)
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS quiz_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        quiz_id INTEGER, 
        student_id INTEGER, 
        start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
        highest_page_reached INTEGER DEFAULT 1, 
        is_completed INTEGER DEFAULT 0, 
        score INTEGER DEFAULT 0, 
        FOREIGN KEY(quiz_id) REFERENCES quizzes(id), 
        FOREIGN KEY(student_id) REFERENCES students(id)
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS student_answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        attempt_id INTEGER, 
        question_id INTEGER, 
        selected_option TEXT, 
        points_awarded INTEGER DEFAULT 0,
        is_graded INTEGER DEFAULT 1,
        FOREIGN KEY(attempt_id) REFERENCES quiz_attempts(id), 
        FOREIGN KEY(question_id) REFERENCES questions(id)
    )''')


    # Safely inject legacy updates
    try: cursor.execute("ALTER TABLE assignments ADD COLUMN file_path TEXT")
    except: pass
    try: cursor.execute("ALTER TABLE submissions ADD COLUMN file_path TEXT")
    except: pass
    try: cursor.execute("ALTER TABLE questions ADD COLUMN question_type TEXT DEFAULT 'MCQ'")
    except: pass
    try: cursor.execute("ALTER TABLE questions ADD COLUMN points INTEGER DEFAULT 1")
    except: pass
    try: cursor.execute("ALTER TABLE student_answers ADD COLUMN points_awarded INTEGER DEFAULT 0")
    except: pass
    try: cursor.execute("ALTER TABLE student_answers ADD COLUMN is_graded INTEGER DEFAULT 1")
    except: pass
    try: cursor.execute("ALTER TABLE teachers ADD COLUMN is_admin INTEGER DEFAULT 0")
    except: pass
    try: cursor.execute("ALTER TABLE quizzes ADD COLUMN due_date TEXT")
    except: pass
    try: cursor.execute("ALTER TABLE students ADD COLUMN is_approved INTEGER DEFAULT 0")
    except: pass
    # Safely inject
    try: cursor.execute("ALTER TABLE students ADD COLUMN email TEXT")
    except: pass
    try: cursor.execute("ALTER TABLE events ADD COLUMN image_path TEXT")
    except: pass
    try: cursor.execute("ALTER TABLE events ADD COLUMN form_link TEXT")
    except: pass
    try: cursor.execute("ALTER TABLE events ADD COLUMN interested_count INTEGER DEFAULT 0")
    except: pass
    try: cursor.execute("ALTER TABLE events ADD COLUMN going_count INTEGER DEFAULT 0")
    except: pass
    try: cursor.execute("ALTER TABLE events ADD COLUMN is_school_wide INTEGER DEFAULT 1")
    except: pass
    try: cursor.execute("ALTER TABLE events ADD COLUMN category TEXT DEFAULT 'General Events'")
    except: pass

    # Authorized teachers / SSLG (see authorized_staff.xlsx)
    cursor.execute('''CREATE TABLE IF NOT EXISTS allowed_staff (staff_id TEXT PRIMARY KEY, full_name TEXT NOT NULL, role TEXT NOT NULL, access_code TEXT)''')
    try: cursor.execute("ALTER TABLE teachers ADD COLUMN role TEXT DEFAULT 'teacher'")
    except: pass
    try: cursor.execute("ALTER TABLE teachers ADD COLUMN full_name TEXT")
    except: pass
    try: cursor.execute("ALTER TABLE teachers ADD COLUMN staff_id TEXT")
    except: pass

    admin_user = os.getenv('ADMIN_USERNAME')
    admin_pass = os.getenv('ADMIN_PASSWORD')
    if admin_user and admin_pass:
        cursor.execute('SELECT * FROM teachers WHERE username = ?', (admin_user,))
        if not cursor.fetchone():
            admin_pw_hash = generate_password_hash(admin_pass)
            cursor.execute('INSERT INTO teachers (username, password_hash, is_admin) VALUES (?, ?, 1)', (admin_user, admin_pw_hash))
        else:
            cursor.execute('UPDATE teachers SET is_admin = 1 WHERE username = ?', (admin_user,))

    conn.commit()
    conn.close()

if __name__ == '__main__':
    setup_database()
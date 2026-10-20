import sqlite3, shutil, os
from datetime import datetime

# 1. Automatic backup first
backup = f"database_backup_{datetime.now():%Y%m%d_%H%M%S}.db"
shutil.copy("database.db", backup)
print("Backup saved as:", backup)

# 2. Ask for confirmation
count_conn = sqlite3.connect("database.db")
n = count_conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
count_conn.close()
print(f"This will delete {n} student accounts and all their quiz answers, submissions, and enrollments.")
if input("Type YES to continue: ") != "YES":
    print("Cancelled. Nothing was deleted.")
    raise SystemExit

# 3. Delete (children first, then students)
conn = sqlite3.connect("database.db")
conn.execute("DELETE FROM student_answers")
conn.execute("DELETE FROM quiz_attempts")
conn.execute("DELETE FROM submissions")
conn.execute("DELETE FROM enrollments")
conn.execute("DELETE FROM students")
conn.execute("DELETE FROM sqlite_sequence WHERE name='students'")  # restart IDs from 1
conn.commit()
conn.close()
print("Done. All student accounts deleted. Admin and teachers were not touched.")

import sqlite3, getpass
from werkzeug.security import generate_password_hash

conn = sqlite3.connect("database.db")
admins = conn.execute("SELECT id, username FROM teachers WHERE is_admin = 1").fetchall()
if not admins:
    print("No admin account found.")
    raise SystemExit

if len(admins) == 1:
    admin = admins[0]
else:
    for a in admins:
        print(a[0], "-", a[1])
    wanted = input("Type the number of the admin to change: ").strip()
    admin = next((a for a in admins if str(a[0]) == wanted), None)
    if not admin:
        print("Not found. Nothing was changed.")
        raise SystemExit

print("Changing the password for admin:", admin[1])
print("(When you type the password, nothing shows on the screen. That is normal.)")
pw1 = getpass.getpass("New password (at least 12 characters): ")
pw2 = getpass.getpass("Type it again: ")

if pw1 != pw2:
    print("The two passwords are different. Nothing was changed.")
elif len(pw1) < 12:
    print("Too short. Use at least 12 characters. Nothing was changed.")
else:
    conn.execute("UPDATE teachers SET password_hash = ? WHERE id = ?", (generate_password_hash(pw1), admin[0]))
    conn.commit()
    print("Done. The admin password was changed.")
conn.close()

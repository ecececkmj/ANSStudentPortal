"""
Creates an Access Code for everyone on the staff list who does not have one yet.

How to use:
  1. Open  AUTHORIZED STAFF.xlsx  and type each person's Full Name and Role (Teacher or SSLG).
     Leave the Access Code cell EMPTY.   Save and CLOSE Excel.
  2. In this folder run:   python make_codes.py
  3. The codes are written into the Excel file and printed here. Send each person THEIR OWN code privately.

Existing codes are never changed. (To give someone a new code, clear their cell and run this again.)
"""
import secrets
import sys

from sync_masterlist import find_staff_file, clean_text_id

ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'      # no 0/O, 1/I/L so codes are easy to read out loud


def new_code():
    pick = lambda n: ''.join(secrets.choice(ALPHABET) for _ in range(n))
    return f'{pick(4)}-{pick(4)}'


def main():
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit('openpyxl is not installed. Run:  pip install openpyxl')

    path = find_staff_file()
    if not path:
        sys.exit('No staff Excel found. Put AUTHORIZED STAFF.xlsx in this folder.')

    try:
        wb = load_workbook(path)
    except Exception as e:
        sys.exit(f'Could not open {path}: {e}\nClose it in Excel and try again.')
    ws = wb.active

    used = {clean_text_id(ws.cell(r, 1).value).lower() for r in range(2, ws.max_row + 1)} - {''}
    made = []
    for r in range(2, ws.max_row + 1):
        name = ws.cell(r, 2).value
        role = ws.cell(r, 3).value
        if not name or not role or clean_text_id(ws.cell(r, 1).value):
            continue
        code = new_code()
        while code.lower() in used:
            code = new_code()
        used.add(code.lower())
        ws.cell(r, 1).value = code
        made.append((str(name).strip(), str(role).strip(), code))

    if not made:
        print('Nobody needs a new code. (Fill in Full Name + Role for new people and leave their Access Code empty.)')
        return
    try:
        wb.save(path)
    except PermissionError:
        sys.exit(f'Could not save {path}. Close it in Excel and run this again.')

    print(f'\nCreated {len(made)} access code(s) and saved them in {path}:\n')
    for name, role, code in made:
        print(f'  {name:<35} {role:<8} {code}')
    print('\nSend each person ONLY their own code, privately.')


if __name__ == '__main__':
    main()

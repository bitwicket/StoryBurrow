#!/usr/bin/env python3
import getpass
import os
import sqlite3
import sys
from pathlib import Path
from werkzeug.security import generate_password_hash

base = Path(os.getenv('STORYBURROW_AUTH_DIR', '/data/auth'))
base.mkdir(parents=True, exist_ok=True, mode=0o700)
db = base / 'users.sqlite3'

if len(sys.argv) != 3 or sys.argv[1] != 'init':
    print('Usage: python /app/authctl.py init USERNAME', file=sys.stderr)
    sys.exit(2)
username = sys.argv[2].strip()
if not username or len(username) > 64 or any(c.isspace() for c in username):
    sys.exit('Username must be 1-64 characters and contain no whitespace.')

with sqlite3.connect(db) as conn:
    conn.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL)')
    if conn.execute('SELECT 1 FROM users LIMIT 1').fetchone():
        sys.exit('An administrator already exists; setup is disabled.')
    password = getpass.getpass('Password (5+ characters): ')
    confirm = getpass.getpass('Confirm password: ')
    if len(password) < 5 or password != confirm:
        sys.exit('Password too short or confirmation did not match.')
    conn.execute('INSERT INTO users(username, password_hash) VALUES (?, ?)', (username, generate_password_hash(password)))
os.chmod(db, 0o600)
print('Administrator created. Visit StoryBurrow HTTPS and sign in.')

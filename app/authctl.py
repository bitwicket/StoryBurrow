#!/usr/bin/env python3
import argparse
import getpass
import os
import sqlite3
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash
from auth_state import reset_password

base = Path(os.getenv('STORYBURROW_AUTH_DIR', '/data/auth'))
base.mkdir(parents=True, exist_ok=True, mode=0o700)
db = base / 'users.sqlite3'

def prompt_password(label='Password'):
    if not sys.stdin.isatty():
        sys.exit('An interactive terminal is required.')
    password = getpass.getpass(f'{label} (6+ characters): ')
    confirm = getpass.getpass('Confirm password: ')
    if password != confirm:
        sys.exit('Passwords do not match.')
    if not 6 <= len(password) <= 1024:
        sys.exit('Password must contain 6-1024 characters.')

    return password

def validate_username(username):
    username = username.strip()
    if not username or len(username) > 64 or any(c.isspace() for c in username):
        sys.exit('Username must be 1-64 characters and contain no whitespace.')
    return username

def init_admin(username):
    password = prompt_password()
    with sqlite3.connect(db, timeout=10) as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS users '
            '(username TEXT PRIMARY KEY, password_hash TEXT NOT NULL)'
        )
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            sys.exit('An administrator already exists; setup is disabled.')
        conn.execute(
            'INSERT INTO users(username, password_hash) VALUES (?, ?)',
            (username, generate_password_hash(password))
        )
    os.chmod(db, 0o600)
    print('Administrator created. Visit StoryBurrow HTTPS and sign in.')

def reset_admin(username):
    password = prompt_password('New password')
    try:
        reset_password(username, password)
    except ValueError as exc:
        sys.exit(str(exc))
    print('Password reset successfully.')
    print('Existing sessions for this account have been invalidated.')

def main():
    parser = argparse.ArgumentParser(
        description='StoryBurrow administrator management'
    )
    parser.add_argument('action', choices=['init', 'reset'])
    parser.add_argument('username')
    args = parser.parse_args()
    username = validate_username(args.username)
    if args.action == 'init':
        init_admin(username)
    elif args.action == 'reset':
        reset_admin(username)

if __name__ == '__main__':
    main()

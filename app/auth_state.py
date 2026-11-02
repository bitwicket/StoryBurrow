import os
import secrets
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash

DB = Path(os.getenv('STORYBURROW_AUTH_DIR', '/data/auth')) / 'users.sqlite3'

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB), timeout=10)
    db.execute('PRAGMA busy_timeout=10000')
    db.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS auth_revisions (username TEXT PRIMARY KEY, revision TEXT NOT NULL)')
    return db

def revision(username):
    if not username:
        return None
    with connect() as db:
        row = db.execute('SELECT revision FROM auth_revisions WHERE username=?', (username,)).fetchone()
        exists = db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone()
    return (row[0] if row else 'legacy') if exists else None

def create_first_admin(username, password):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            raise ValueError('An administrator already exists')
        db.execute('INSERT INTO users (username,password_hash) VALUES (?,?)',
                   (username, generate_password_hash(password)))
        rev = secrets.token_urlsafe(24)
        db.execute('INSERT INTO auth_revisions (username,revision) VALUES (?,?)', (username,rev))
    return rev

def reset_password(username, password):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        updated = db.execute('UPDATE users SET password_hash=? WHERE username=?',
                             (generate_password_hash(password), username))
        if updated.rowcount != 1:
            raise ValueError('Unknown administrator account')
        rev = secrets.token_urlsafe(24)
        db.execute('INSERT INTO auth_revisions (username,revision) VALUES (?,?) ON CONFLICT(username) DO UPDATE SET revision=excluded.revision',
                   (username, rev))

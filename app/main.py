import hmac
import secrets
import sqlite3
import os
import socket
import json
import subprocess
import threading
import re
from pathlib import Path
from urllib.parse import urlsplit
from functools import wraps
from werkzeug.security import check_password_hash

from flask import Flask, abort, redirect, render_template, request, send_file, url_for, session, flash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

app = Flask(__name__)
from first_run import bp as first_run_bp
app.register_blueprint(first_run_bp)
from import_routes import bp as import_bp
app.register_blueprint(import_bp)
from import_art_routes import bp as import_art_bp
app.register_blueprint(import_art_bp)
GAMES = Path(os.getenv('STORYBURROW_GAMES_DIR', '/games')).resolve()
MANAGER_SOCKET = os.getenv('STORYBURROW_MANAGER_SOCKET', '/tmp/runtime-ubuntu/storyburrow-manager.sock')
PLAYER_URL = os.getenv('STORYBURROW_PLAYER_URL', '').strip()
# Persist both the user database and Flask signing key outside the image.
AUTH_DIR = Path(os.getenv('STORYBURROW_AUTH_DIR', '/data/auth'))
AUTH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
SECRET_FILE = AUTH_DIR / 'session.key'
if not SECRET_FILE.exists():
    try:
        with SECRET_FILE.open('x') as f:
            f.write(secrets.token_hex(32))
        SECRET_FILE.chmod(0o600)
    except FileExistsError:
        pass
app.secret_key = SECRET_FILE.read_text().strip()
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=60*60*24*7,
                  MAX_CONTENT_LENGTH=1024*1024)
DB_PATH = AUTH_DIR / 'users.sqlite3'
TOKENS = URLSafeTimedSerializer(app.secret_key, salt='storyburrow-play')
COVERS = ('cover.webp', 'cover.png', 'cover.jpg', 'poster.webp', 'poster.png', 'poster.jpg')
INPUT_LOCK = threading.Lock()
WINDOW_PATTERN = re.compile(r'^\s*(0x[0-9a-fA-F]+)\s+\"([^\"]*)\".*?\s+(\d+)x(\d+)[+-]')

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL)')
    return conn

def has_admin():
    with db_connect() as conn:
        return conn.execute('SELECT 1 FROM users LIMIT 1').fetchone() is not None

def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_urlsafe(32)
    return session['_csrf']

def valid_csrf():
    return hmac.compare_digest(request.form.get('csrf_token', ''), session.get('_csrf', 'not-set'))

def safe_next(value):
    parsed = urlsplit(value or '')
    return value if value and value.startswith('/') and not value.startswith('//') and not parsed.scheme and not parsed.netloc else url_for('library')

@app.before_request
def protect():
    if request.endpoint == 'patches.upload':
        request.max_content_length = 256 * 1024 * 1024
    if request.endpoint == 'custom_art.set_custom_artwork':
        request.max_content_length = 13 * 1024 * 1024
    if request.endpoint == 'imports.upload_create':
        request.max_content_length = 4 * 1024**2  # JSON manifest, up to 4 MiB
    elif request.endpoint == 'imports.upload_chunk':
        request.max_content_length = 20 * 1024**2  # 16 MiB chunk + headroom
    elif request.endpoint in ('imports.upload_complete', 'imports.upload_delete', 'imports.import_discard', 'imports.import_vndb'):
        request.max_content_length = 1024 * 1024
    if request.endpoint == 'first_run.setup':
        return None
    if request.endpoint in ('health', 'login', 'static'):
        return None
    if not has_admin():
        return redirect(url_for('first_run.setup'), code=303)
    from auth_state import revision
    if session.get('user') and session.get('auth_revision', 'legacy') != revision(session['user']):
        session.clear()
    if request.endpoint == 'stream_auth':
        return None
    if not session.get('user'):
        if request.path.startswith('/covers/'):
            abort(401)
        return redirect(url_for('login', next=request.path if request.method == 'GET' else '/'))
    return None

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not has_admin():
        return redirect(url_for('first_run.setup'), code=303)
    if session.get('user'):
        return redirect(url_for('library'))
    error = None
    if request.method == 'POST':
        if not valid_csrf():
            abort(400, 'Invalid form token')
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        with db_connect() as conn:
            row = conn.execute('SELECT password_hash FROM users WHERE username=?', (username,)).fetchone()
        if row and check_password_hash(row[0], password):
            session.clear()
            session['user'] = username
            from auth_state import revision
            session['auth_revision'] = revision(username)
            session.permanent = True
            return redirect(safe_next(request.form.get('next')), code=303)
        error = 'Invalid username or password.'
    return render_template('login.html', error=error, csrf=csrf_token(), next=safe_next(request.values.get('next')))

@app.post('/logout')
def logout():
    if not valid_csrf():
        abort(400)
    session.clear()
    return redirect(url_for('login'), code=303)

def manager_command(action, **kwargs):
    payload = json.dumps({'action': action, **kwargs}).encode() + b'\n'
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(5)
            conn.connect(MANAGER_SOCKET)
            conn.sendall(payload)
            with conn.makefile('rb') as stream:
                response = stream.readline(65537)
            if not response or len(response) > 65536:
                raise ValueError('Invalid response from game manager')
            result = json.loads(response)
            if not isinstance(result, dict):
                raise ValueError('Invalid manager response')
            return result
    except (OSError, ValueError) as exc:
        app.logger.warning('Game manager unavailable: %s', exc)
        return {'ok': False, 'error': 'Game manager unavailable'}

def installed_games():
    result = manager_command('list')
    if not result.get('ok'):
        return [], result.get('error', 'Game manager unavailable')
    games = []
    for item in result.get('games', []):
        game_id = item.get('id', '')
        directory = (GAMES / game_id).resolve()
        if directory.parent != GAMES or not directory.is_dir():
            continue
        cover = next((name for name in COVERS
                      if (directory / name).is_file() and not (directory / name).is_symlink()), None)
        games.append({'id': game_id,
                      'title': game_id.replace('_', ' ').replace('-', ' '),
                      'cover': cover})
    try:
        from catalog import initialize, installed_by_directory
        initialize()
        by_dir = installed_by_directory()
        for item in games:
            record = by_dir.get(item['id'])
            if record:
                item['title'] = record['display_title']
                item['version'] = record['installed_version']
                item['vndb_id'] = record['vndb_id']
    except (OSError, ValueError, RuntimeError, sqlite3.DatabaseError) as exc:
        app.logger.warning('Catalog metadata unavailable: %s', exc)
    # Cached artwork is optional and never fetched over the network during page load.
    try:
        from artwork import cover_for_directory
        for game in games:
            if cover_for_directory(game['id']):
                game['cover'] = 'cached-artwork'
        from custom_artwork import current_override
        for game in games: 
            if current_override(game['id']):
                game['cover'] = 'custom-artwork'
    except Exception:
        app.logger.exception('Could not load cached artwork overlay')
    return games, None

@app.get('/')
def library():
    games, error = installed_games()
    status = manager_command('status')
    return render_template('library.html', games=games, error=error,
                           status=status, csrf=TOKENS.dumps('play'), logout_csrf=csrf_token(), player_url=PLAYER_URL, import_csrf=csrf_token())

@app.post('/play/<game_id>')
def play(game_id):
    try:
        if TOKENS.loads(request.form.get('csrf_token', ''), max_age=3600) != 'play':
            abort(400)
    except (BadSignature, SignatureExpired):
        abort(400, 'Form expired. Reload the library and try again.')
    # Re-check installed games; don't let user-supplied IDs address arbitrary paths.
    games, error = installed_games()
    if error:
        return render_template('message.html', title='Manager unavailable', message=error), 503
    if game_id not in {game['id'] for game in games}:
        abort(404)
    result = manager_command('start', game=game_id)
    if not result.get('ok'):
        return render_template('message.html', title='Could not launch game',
                               message=result.get('error', 'Unknown error')), 409
    return redirect(url_for('player'), code=303)

@app.post('/stop')
def stop_game():
    # protect() already requires a logged-in user for this route.
    if not valid_csrf():
        abort(400, 'Invalid form token')
    result = manager_command('stop')
    if not result.get('ok'):
        return render_template(
            'message.html',
            title='Could not stop game',
            message=result.get('error', 'Unknown error'),
        ), 409
    return redirect(url_for('library'), code=303)

@app.get('/player')
def player():
    status = manager_command('status')
    if not status.get('ok'):
        return render_template('message.html', title='Manager unavailable',
                               message=status.get('error', 'Unavailable')), 503
    if not status.get('running'):
        return redirect(url_for('library'))
    return render_template('player.html', game=status.get('game'), input_csrf=csrf_token())

def game_window():
    #Locate visible game window
    env = {**os.environ, 'DISPLAY': os.environ.get('DISPLAY', ':20')}
    result = subprocess.run(['xwininfo', '-root', '-tree'], env=env,
                            text=True, capture_output=True, check=True, timeout=3)
    windows = []
    for line in result.stdout.splitlines():
        match = WINDOW_PATTERN.match(line)
        if match and int(match.group(3)) >= 200 and int(match.group(4)) >= 150:
            windows.append(match.group(1))
    if len(windows) != 1:
        raise ValueError(f'Expected one game window; found {len(windows)}')
    return windows[0], env

def x11_key(env, key):
    subprocess.run(['xdotool', 'key', '--clearmodifiers', key], env=env,
                   check=True, capture_output=True, timeout=4)

@app.post('/api/player/input')
def player_input():
    provided = request.headers.get('X-CSRF-Token', '')
    if not hmac.compare_digest(provided, session.get('_csrf', 'not-set')):
        abort(400, 'Invalid form token')
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {'ok': False, 'error': 'JSON object required'}, 400
    action = data.get('action')
    if action not in ('menu', 'rollback', 'forward', 'text'):
        return {'ok': False, 'error': 'Unsupported input action'}, 400
    value = data.get('text', '')
    if action == 'text':
        if not isinstance(value, str) or len(value) > 80 or any(ord(c) < 32 or ord(c) > 126 for c in value):
            return {'ok': False, 'error': 'Text must be up to 80 printable ASCII characters'}, 400
    status = manager_command('status')
    if not status.get('ok'):
        return {'ok': False, 'error': 'Game manager unavailable'}, 503
    if not status.get('running'):
        return {'ok': False, 'error': 'No running game'}, 409
    # A client must belong to the same session it opened.
    if status.get('game') != data.get('game'):
        return {'ok': False, 'error': 'Game session changed. Reload player.'}, 409
    if not INPUT_LOCK.acquire(blocking=False):
        return {'ok': False, 'error': 'Another input action is still running'}, 429
    try:
        window, env = game_window()
        subprocess.run(['xdotool', 'windowfocus', '--sync', window], env=env,
                       check=True, capture_output=True, timeout=4)
        if action == 'menu':
            x11_key(env, 'Escape')
        elif action == 'rollback':
            x11_key(env, 'Prior')
        elif action == 'forward':
            x11_key(env, 'Next')
        elif value == '':
            # Accept default name unchanged.
            x11_key(env, 'Return')
        else:
            # Standard Ren'Py input does not reliably implement Ctrl+A.
            x11_key(env, 'End')
            subprocess.run(['xdotool', 'key', '--clearmodifiers', '--repeat', '100',
                            '--delay', '12', 'BackSpace'], env=env,
                           check=True, capture_output=True, timeout=5)
            subprocess.run(['xdotool', 'type', '--clearmodifiers', '--delay', '35', '--', value],
                           env=env, check=True, capture_output=True, timeout=7)
            x11_key(env, 'Return')
        return {'ok': True}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        app.logger.warning('Player input failed: %s', exc)
        return {'ok': False, 'error': 'Could not deliver input to game'}, 503
    finally:
        INPUT_LOCK.release()


@app.get('/covers/<game_id>')
def cover(game_id):
    games, error = installed_games()
    if error or game_id not in {game['id'] for game in games}:
        abort(404)
    from custom_artwork import current_override
    custom = current_override(game_id)
    if custom:
        return send_file(custom, max_age=0)
    from artwork import cover_for_directory
    cached = cover_for_directory(game_id)
    if cached:
        return send_file(cached, max_age=300)
    directory = (GAMES / game_id).resolve()
    for name in COVERS:
        candidate = directory / name
        if candidate.is_file() and not candidate.is_symlink():
            return send_file(candidate, max_age=300)
    abort(404)

@app.get('/health')
def health():
    return {'status': 'ok'}

@app.get('/internal/stream-auth')
def stream_auth():
    if not has_admin():
        return '', 503
    if not session.get('user'):
        return '', 401
    return '', 204

@app.get('/api/session')
def session_status():
    # Flask's protect() requires StoryBurrow login for this endpoint.
    status = manager_command('status')
    if not status.get('ok'):
        return {'ok': False, 'error': status.get('error', 'Unavailable')}, 503
    return {'ok': True, 'running': bool(status.get('running')),
            'game': status.get('game')}, 200

# Register editing after core routes/helpers are defined.
from edit_routes import bp as edit_bp
app.register_blueprint(edit_bp)

from patch_routes import bp as patch_bp
app.register_blueprint(patch_bp)

from update_routes import bp as update_bp
app.register_blueprint(update_bp)

from save_manager_routes import bp as save_manager_bp
app.register_blueprint(save_manager_bp)

from save_actions_routes import bp as save_actions_bp
app.register_blueprint(save_actions_bp)
from save_operations import start_cleanup_worker
start_cleanup_worker(manager_command)

from save_editor_routes import bp as save_editor_bp
app.register_blueprint(save_editor_bp)

from adoption_routes import bp as adoption_bp
app.register_blueprint(adoption_bp)

from storage_routes import bp as storage_bp
app.register_blueprint(storage_bp)

from snapshot_import_routes import bp as snapshot_import_bp
app.register_blueprint(snapshot_import_bp)

from build_backup_routes import bp as build_backup_bp, cleanup_expired
app.register_blueprint(build_backup_bp)

from custom_artwork import bp as custom_art_bp
app.register_blueprint(custom_art_bp)

from import_url_routes import bp as import_url_bp
app.register_blueprint(import_url_bp)

@app.before_request
def _storyburrow_build_cleanup():
    # On-demand expiration; never acts while any game is running.
    if request.endpoint not in ('static', 'health'):
        cleanup_expired()
import hmac
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, session, send_file, url_for
from werkzeug.exceptions import HTTPException
from save_actions_routes import guarded_root

bp = Blueprint('storage', __name__)
NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$')

@bp.errorhandler(HTTPException)
def error_page(e):
    return render_template('message.html', title='Storage unavailable', message=e.description), e.code

def _snapshots(game_id):
    root = guarded_root(game_id)  # Checks catalog, managed routing, manager availability, and stopped state.
    folder = root / 'snapshots'
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        abort(409, description='Unsafe snapshot storage')
    return folder

def _snapshot(game_id, label):
    if not NAME.fullmatch(label) or label in ('.','..') or label.startswith('.'):
        abort(404)
    folder = _snapshots(game_id)
    path = folder / label
    if path.is_symlink() or not path.is_dir():
        abort(404)
    return path

def _entries(path):
    total = 0
    members = []
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            item = Path(base) / name
            if item.is_symlink():
                raise ValueError('Snapshot contains a symlink; operation refused')
            if item.is_file():
                if item.stat().st_nlink != 1:
                    raise ValueError('Snapshot contains a hard-linked file')
                members.append((item, item.relative_to(path).as_posix()))
                total += item.stat().st_size
            elif not item.is_dir():
                raise ValueError('Snapshot contains unsupported content')
    return members, total

@bp.get('/games/<game_id>/storage')
def storage_page(game_id):
    _snapshots(game_id)  # Enforce managed game and stopped state.
    return redirect(f'/games/{game_id}/saves?tab=snapshots', code=302)

@bp.get('/games/<game_id>/storage/snapshots/<label>/download')
def download_snapshot(game_id, label):
    path = _snapshot(game_id, label)
    try:
        members, _ = _entries(path)
        # Spooled temporary file avoids holding a multi-MB snapshot entirely in RAM.
        tmp = tempfile.SpooledTemporaryFile(max_size=8*1024*1024, mode='w+b')
        with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for item, relative in members:
                archive.write(item, relative)
        tmp.seek(0)
        response = send_file(tmp, as_attachment=True, download_name=f'{game_id}-{label}.zip', mimetype='application/zip', max_age=0)
        response.call_on_close(tmp.close)
        return response
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        abort(409, description=str(exc))

@bp.post('/games/<game_id>/storage/snapshots/<label>/delete')
def delete_snapshot(game_id, label):
    if not hmac.compare_digest(request.form.get('csrf_token',''), session.get('_csrf','not-set')):
        abort(400, description='Invalid form token')
    if request.form.get('confirm') != 'delete':
        abort(400, description='Type delete to confirm permanent snapshot deletion')
    path = _snapshot(game_id, label)
    try:
        _entries(path)  # Do not recursively delete symlinks or unusual files.
        shutil.rmtree(path)
    except (OSError, ValueError) as exc:
        abort(409, description=f'Could not delete snapshot: {exc}')
    return redirect(f'/games/{game_id}/saves?tab=snapshots', code=303)

import os
import re
from datetime import datetime, timezone
from save_time import local_time
# STORYBURROW_STAGE4C_POLISH
from pathlib import Path
from flask import Blueprint, abort, render_template, Response
from werkzeug.exceptions import HTTPException

bp = Blueprint('save_manager', __name__)
SAVES = Path(os.environ.get('STORYBURROW_SAVES_DIR', '/saves')).resolve()
SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$')
MAX_ENTRIES = 1000
# STORYBURROW_STAGE4B
from save_metadata import read_metadata, read_screenshot


def _safe_directory(root, parent):
    return root.is_dir() and not root.is_symlink() and root.resolve().parent == parent.resolve()


def _entries(root):
    # Bounded top-level inventory only; never follow links or inspect pickle contents.
    results = []
    if not _safe_directory(root, root.parent):
        return results
    for child in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if len(results) >= MAX_ENTRIES:
            break
        if child.is_symlink() or not child.is_file():
            continue
        try:
            st = child.stat(follow_symlinks=False)
        except OSError:
            continue
        extra = read_metadata(child) if child.suffix.lower() == '.save' else {}
        results.append({'name': child.name, **extra, 'bytes': st.st_size,
                        'size': _size(st.st_size),
                        'modified': local_time(st.st_mtime),
                        'kind': ('Persistent' if child.name == 'persistent' else
                                 'Ren’Py save' if child.suffix == '.save' else 'Other file')})
    return results


def _size(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if n < 1024 or unit == 'GiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


@bp.get('/games/<game_id>/saves')
def saves_page(game_id):
    from main import installed_games, manager_command
    from catalog import connect, initialize
    from save_routing import _read_config
    status = manager_command('status')
    if not status.get('ok'):
        abort(503, description='Game manager unavailable; save browsing is locked')
    if status.get('running'):
        abort(409, description='Stop the running game before opening Save Manager')
    games, error = installed_games()
    if error:
        abort(503, description=error)
    game = next((g for g in games if g['id'] == game_id), None)
    if not game or not SAFE_NAME.fullmatch(game_id):
        abort(404)
    initialize()
    with connect() as db:
        row = db.execute("""SELECT installation_id, managed_save_namespace, save_mode
            FROM installations WHERE directory_name=? AND status='installed'""", (game_id,)).fetchone()
    if row is None or row['save_mode'] != 'managed' or not row['managed_save_namespace']:
        return render_template('save_manager.html', game=game, unsupported=True,
                               saves=[], snapshots=[], namespace=None)
    namespace = row['managed_save_namespace']
    if not SAFE_NAME.fullmatch(namespace):
        abort(409, description='Invalid managed save namespace')
    root = SAVES / namespace
    if not _safe_directory(root, SAVES):
        abort(409, description='Managed save namespace is missing or unsafe')
    route = _read_config().get(game_id)
    if not isinstance(route, dict) or route.get('namespace') != namespace:
        abort(409, description='Managed save routing disagrees with catalog')
    current = root / 'current'
    if not _safe_directory(current, root):
        abort(409, description='Managed current save directory is missing or unsafe')
    from save_operations import cleanup, recoveries, LOCK
    with LOCK:
        cleanup(root)
        recovery_items = recoveries(root)
    snapshots = []
    snap_root = root / 'snapshots'
    if snap_root.exists() or snap_root.is_symlink():
        if not _safe_directory(snap_root, root):
            abort(409, description='Snapshot directory is unsafe')
        for child in sorted(snap_root.iterdir(), key=lambda p: p.name, reverse=True):
            if len(snapshots) >= MAX_ENTRIES:
                break
            if not _safe_directory(child, snap_root):
                continue
            try:
                timestamp = local_time(child.stat().st_mtime)
            except OSError:
                timestamp = 'Unknown'
            snapshots.append({'name': child.name, 'modified': timestamp,
                              'files': _entries(child)})
    return render_template('save_manager.html', game=game, unsupported=False,
                           saves=_entries(current), snapshots=snapshots,
                           namespace=namespace, recoveries=recovery_items,
                           csrf=__import__('main').csrf_token(),
                           copy_targets=[g for g in games if g['id'] != game_id])


@bp.errorhandler(HTTPException)
def save_manager_error(error):
    titles = {409: 'Save Manager unavailable', 503: 'Game manager unavailable',
              404: 'Game not found', 403: 'Save Manager unavailable'}
    return render_template('message.html',
                           title=titles.get(error.code, error.name),
                           message=error.description), error.code


@bp.get('/games/<game_id>/saves/thumbnail/<path:filename>')
def save_thumbnail(game_id, filename):
    from main import manager_command, installed_games
    from catalog import connect
    from save_routing import _read_config
    status = manager_command('status')
    if not status.get('ok'):
        abort(503, description='Game manager unavailable; save browsing is locked')
    if status.get('running'):
        abort(409, description='Stop the running game before opening Save Manager')
    games, error = installed_games()
    if error:
        abort(503, description=error)
    if game_id not in {g['id'] for g in games} or not SAFE_NAME.fullmatch(game_id):
        abort(404)
    if '/' in filename or '\\' in filename or filename in ('.','..') or not filename.endswith('.save') or len(filename)>200:
        abort(404)
    with connect() as db:
        row = db.execute("SELECT managed_save_namespace, save_mode FROM installations WHERE directory_name=? AND status='installed'", (game_id,)).fetchone()
    if row is None or row['save_mode'] != 'managed':
        abort(404)
    ns = row['managed_save_namespace']
    if not ns or not SAFE_NAME.fullmatch(ns) or _read_config().get(game_id, {}).get('namespace') != ns:
        abort(409, description='Managed save routing mismatch')
    root = SAVES / ns
    current = root / 'current'
    if not _safe_directory(root, SAVES) or not _safe_directory(current, root):
        abort(409, description='Managed save storage is unsafe')
    file = current / filename
    if file.is_symlink() or not file.is_file():
        abort(404)
    try:
        data = read_screenshot(file)
    except (OSError, ValueError, RuntimeError):
        abort(404, description='Save screenshot unavailable')
    return Response(data, content_type='image/png', headers={'Cache-Control':'private, no-store', 'X-Content-Type-Options':'nosniff'})

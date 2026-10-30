import hmac
import os
import tempfile
from pathlib import Path
from flask import Blueprint, abort, request, session, redirect, url_for, send_file, render_template, flash
from werkzeug.exceptions import HTTPException
from save_operations import LOCK, safe_file, directory, _file, replace, delete, restore_snapshot, restore_recovery, recoveries, cleanup

bp = Blueprint('save_actions', __name__)
SAVES = Path(os.environ.get('STORYBURROW_SAVES_DIR', '/saves')).resolve()

@bp.errorhandler(HTTPException)
def error_page(e):
    return render_template('message.html', title='Save Manager unavailable', message=e.description), e.code

def guarded_root(game_id):
    from main import manager_command, installed_games
    from catalog import connect
    from save_routing import _read_config
    from save_manager_routes import SAFE_NAME
    status = manager_command('status')
    if not status.get('ok'):
        abort(503, description='Game manager unavailable')
    if status.get('running'):
        abort(409, description='Stop the running game before managing saves')
    if not SAFE_NAME.fullmatch(game_id):
        abort(404)
    games, err = installed_games()
    if err:
        abort(503, description='Library unavailable')
    if game_id not in {g['id'] for g in games}:
        abort(404)
    with connect() as db:
        row = db.execute("SELECT managed_save_namespace, save_mode FROM installations WHERE directory_name=? AND status='installed'", (game_id,)).fetchone()
    if not row or row['save_mode'] != 'managed':
        abort(409, description='This installation has no managed saves')
    ns = row['managed_save_namespace']
    if not ns or not SAFE_NAME.fullmatch(ns):
        abort(409, description='Invalid save namespace')
    route = _read_config().get(game_id)
    if not isinstance(route, dict) or route.get('namespace') != ns:
        abort(409, description='Save routing disagrees with catalog')
    root = SAVES / ns
    if not directory(root, SAVES) or not directory(root/'current', root):
        abort(409, description='Managed save storage is missing or unsafe')
    return root

def csrf():
    if not hmac.compare_digest(request.form.get('csrf_token',''), session.get('_csrf','not-set')):
        abort(400, description='Invalid request token')

def go(game_id):
    return redirect(url_for('save_manager.saves_page', game_id=game_id), code=303)

def invalid(e):
    abort(409, description=str(e))

@bp.get('/games/<game_id>/saves/export/<filename>')
def export(game_id, filename):
    root = guarded_root(game_id)
    try:
        name = safe_file(filename)
        file = root/'current'/name
        if not _file(file):
            abort(404)
    except ValueError as e:
        invalid(e)
    return send_file(file, as_attachment=True, download_name=name, mimetype='application/octet-stream', max_age=0)

@bp.post('/games/<game_id>/saves/import')
def import_save(game_id):
    csrf()
    # Guard BEFORE accepting any save bytes into application storage.
    with LOCK:
        root = guarded_root(game_id)
        upload = request.files.get('save')
        if upload is None:
            abort(400, description='Select a save file')
        try:
            name = safe_file(upload.filename)
            if request.content_length is None or request.content_length > 128 * 1024 * 1024:
                abort(413, description='Save upload exceeds 128 MiB')
            with tempfile.TemporaryDirectory(prefix='storyburrow-save-upload-') as tmpdir:
                staged = Path(tmpdir)/'uploaded'
                with staged.open('wb') as f:
                    length = 0
                    while True:
                        chunk = upload.stream.read(1024*1024)
                        if not chunk: break
                        length += len(chunk)
                        if length > 128*1024*1024:
                            abort(413, description='Save upload exceeds 128 MiB')
                        f.write(chunk)
                if length == 0: raise ValueError('Empty save upload')
                guarded_root(game_id) # Recheck manager state immediately before write.
                replaced = replace(root, name, staged, 'import')
        except (ValueError, OSError) as e:
            invalid(e)
    if replaced:
        flash('Save replaced. The previous file is available under Recently recoverable for 24 hours.', 'success')
    else:
        flash('Save imported.', 'success')
    return go(game_id)

@bp.post('/games/<game_id>/saves/delete')
def delete_save(game_id):
    csrf()
    with LOCK:
        root = guarded_root(game_id)
        try:
            delete(root, request.form.get('filename'))
        except (ValueError, OSError) as e: invalid(e)
    return go(game_id)

@bp.post('/games/<game_id>/saves/snapshot-restore')
def snapshot_restore(game_id):
    csrf()
    with LOCK:
        root = guarded_root(game_id)
        name = request.form.get('filename') or None
        try:
            restore_snapshot(root, request.form.get('snapshot'), name)
        except (ValueError, OSError) as e: invalid(e)
    return go(game_id)

@bp.post('/games/<game_id>/saves/recover')
def recover(game_id):
    csrf()
    with LOCK:
        root = guarded_root(game_id)
        try:
            restore_recovery(root, request.form.get('token'))
        except (ValueError, OSError) as e: invalid(e)
    return go(game_id)

@bp.post('/games/<game_id>/saves/copy')
def copy_save(game_id):
    csrf()
    with LOCK:
        source_root = guarded_root(game_id)
        target_game = request.form.get('target_game')
        if target_game == game_id:
            abort(400, description='Choose a different installation')
        target_root = guarded_root(target_game)
        try:
            name = safe_file(request.form.get('filename'))
            source = source_root/'current'/name
            if not _file(source): raise ValueError('Source save is missing or unsafe')
            # Intentionally do not overwrite an existing save in target installation.
            target = target_root/'current'/name
            if target.exists() or target.is_symlink():
                raise ValueError('Target already has that save filename; copy refused')
            replace(target_root, name, source, 'copy')
        except (ValueError, OSError) as e: invalid(e)
    return go(game_id)

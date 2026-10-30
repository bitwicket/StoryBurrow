import hmac
import json
import os
import shutil
import threading
import uuid
import time
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request, session
from catalog import connect, installed_by_directory
from installer import prepare, GAMES
from importer.inspector import inspect
from import_routes import ROOT, LOCK, get_upload, save_state, TOKEN
from managed_saves import snapshot
from save_inspector import inspect_save_directory
from save_routing import _read_config, SAVES

bp = Blueprint('updates', __name__)
UPDATE_LOCK = threading.Lock()


def _csrf():
    if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), session.get('_csrf', 'not-set')):
        abort(400, description='Invalid CSRF token')


def _installation(name):
    from main import manager_command
    status = manager_command('status')
    if not status.get('ok') or status.get('running'):
        abort(409, description='Stop the running game before updating')
    record = installed_by_directory().get(name)
    if not record:
        abort(404, description='Catalog-managed installation not found')
    with connect() as db:
        row = db.execute('SELECT * FROM installations WHERE directory_name=? AND status=?', (name, 'installed')).fetchone()
    if row is None or row['save_mode'] != 'managed' or not row['managed_save_namespace']:
        abort(409, description='Managed saves are required for game updates')
    return dict(row), record


def _validate_route(name, row):
    from save_routing import _paths
    config = _read_config().get(name)
    if not config or config.get('namespace') != row['managed_save_namespace']:
        raise ValueError('Managed routing does not match installation database')
    directory, root = _paths(name, row['managed_save_namespace'])
    current = root / 'current'
    parent = root / 'renpy-parent'
    local = directory / 'game' / 'saves'
    if not current.is_dir() or current.is_symlink() or not parent.is_dir() or parent.is_symlink():
        raise ValueError('Managed save storage is missing or unsafe')
    if not local.is_symlink() or local.resolve() != current.resolve():
        raise ValueError('Game-local saves are not routed to managed storage')
    return directory, root, config


@bp.get('/games/<game_id>/update')
def update_page(game_id):
    row, record = _installation(game_id)
    from main import csrf_token
    return render_template('update_game.html', game_id=game_id,
                           current_version=row.get('installed_version') or '',
                           csrf=csrf_token())


@bp.post('/api/games/<game_id>/update/<token>')
def update_begin(game_id, token):
    _csrf()
    if not TOKEN.fullmatch(token):
        abort(404)
    if not UPDATE_LOCK.acquire(blocking=False):
        return jsonify(ok=False, error='Another update is underway'), 409
    launched = False
    try:
        row, record = _installation(game_id)
        _validate_route(game_id, row)
        body = request.get_json(silent=True) or {}
        version = body.get('version')
        retain_build = body.get('retain_build', True)
        if not isinstance(retain_build, bool):
            return jsonify(ok=False, error='Invalid backup retention choice'), 400
        if not isinstance(version, str) or not (0 < len(version.strip()) <= 100):
            return jsonify(ok=False, error='Enter a version for the incoming build'), 400
        version = version.strip()
        with LOCK:
            root, state = get_upload(token)
            if state['status'] != 'ready' or not (root / 'inspection.json').is_file():
                return jsonify(ok=False, error='Upload is not inspected and ready'), 409
            state['status'] = 'updating'
            state['update_error'] = None
            state['update_result'] = None
            save_state(root, state)
        threading.Thread(target=_worker, args=(root, token, state['owner'], game_id, version, retain_build), daemon=True).start()
        launched = True
        return jsonify(ok=True, status='updating'), 202
    finally:
        # The worker holds the exclusive logical update slot until completion.
        # Release only if the thread wasn't launched.
        if not launched:
            UPDATE_LOCK.release()


def _worker(root, token, owner, name, version, retain_build):
    pending = None
    backup = None
    swapped = False
    reserved = False
    result = None
    try:
        from main import manager_command
        claimed = manager_command('reserve_update', game=name)
        if not claimed.get('ok'):
            raise ValueError(claimed.get('error', 'Could not reserve game for update'))
        reserved = True
        with connect() as db:
            row = db.execute('SELECT * FROM installations WHERE directory_name=? AND status=?', (name, 'installed')).fetchone()
            if row is None:
                raise ValueError('Installation disappeared')
            row = dict(row)
        original, base, route = _validate_route(name, row)
        with LOCK:
            state = json.loads((root / 'state.json').read_text('utf-8'))
            if state['owner'] != owner or state['status'] != 'updating':
                raise ValueError('Upload ownership/state changed')
            source = root / ('upload.zip' if state['kind'] == 'zip' else 'folder')
            inspected = json.loads((root / 'inspection.json').read_text('utf-8'))
        if not source.exists():
            raise ValueError('Uploaded build is missing')
        # `prepare` is shared with Import Game, which rejects an existing target.
        # Supply an unused validation name: `prepare` still returns a staging
        # directory under /games/.storyburrow-installing, not a new game install.
        staging_name = 'StoryBurrowUpdate-' + uuid.uuid4().hex
        pending, launcher, fresh = prepare(source, staging_name)
        if fresh['detected']['launcher'] != inspected['detected']['launcher']:
            raise ValueError('Uploaded launcher changed after inspection')
        identity = inspect_save_directory(pending)
        incoming_id = identity.get('save_directory')
        original_id = route.get('save_id')
        if not original_id or not incoming_id:
            raise ValueError('Cannot verify Ren\'Py save identifier for both versions. Update blocked until routing compatibility is established.')
        if original_id != incoming_id:
            raise ValueError('Ren\'Py save identifier changed. Update blocked to prevent misrouted saves; use a separate import.')
        # New build can contain a stale game-local saves directory; never merge it.
        local = pending / 'game' / 'saves'
        if local.is_symlink():
            raise ValueError('Incoming game contains a symlinked saves directory')
        if local.exists():
            # Refuse unexpected bundled save data rather than silently destroying it.
            if not local.is_dir() or any(local.iterdir()):
                raise ValueError('Incoming game contains game-local save data. Remove those files from the upload first.')
            local.rmdir()
        # The old version must be recorded, not guessed from the incoming upload.
        previous_version = (row.get('installed_version') or '').strip()
        if not previous_version:
            raise ValueError('Set the installed version in Edit Game before updating (needed for snapshot label)')
        saved_snapshot = snapshot(SAVES, row['managed_save_namespace'], previous_version)
        # Route the replacement's local saves to the original UUID-owned directory.
        local.symlink_to(base / 'current', target_is_directory=True)
        backups = GAMES / '.storyburrow-update-backups'
        if backups.is_symlink():
            raise ValueError('Update backup root cannot be a symlink')
        backups.mkdir(exist_ok=True)
        backup = backups / uuid.uuid4().hex
        switched = manager_command('replace_update', game=name, pending=str(pending), backup=str(backup))
        if not switched.get('ok'):
            raise ValueError(switched.get('error', 'Could not swap game files'))
        pending = None
        swapped = True
        with connect() as db:
            cur = db.execute('''UPDATE installations SET installed_version=?, launcher_name=?, source_type=?,
                source_filename=?, installed_at=? WHERE installation_id=? AND directory_name=? AND status='installed' ''',
                (version, launcher, 'update_upload', state['files'][0]['path'], __import__('time').time_ns() // 10**9,
                 row['installation_id'], name))
            if cur.rowcount != 1:
                raise ValueError('Installation catalog changed during update')
            db.execute('''INSERT INTO metadata_fields (game_id,field_name,field_value_json,source_type,source_ref,updated_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(game_id,field_name) DO UPDATE SET
                field_value_json=excluded.field_value_json,source_type=excluded.source_type,updated_at=excluded.updated_at''',
                (row['game_id'], 'installed_version', json.dumps(version), 'update', None,
                 __import__('time').time_ns() // 10**9))
        # Only backups created by this version receive an expiration marker.
        # The previous build stays available for rollback until catalog commit.
        if retain_build:
            from build_backups import register_backup
            register_backup(backup, row['installation_id'], name, previous_version, version, row['launcher_name'])
        else:
            # Removal is best-effort: an unexpected cleanup failure must not
            # turn a successful update into a failed operation.
            from build_backups import remove_unretained
            remove_unretained(backup)
        result = {'directory':name, 'installed_version':version, 'previous_version':previous_version,
                  'installation_id':row['installation_id'], 'snapshot':str(saved_snapshot),
                  'previous_build':str(backup) if retain_build else None,
                   'backup_expires_in_hours':48 if retain_build else None}
    except Exception as exc:
        # Never remove the previous build or any save namespace on failure.
        error = str(exc)
        if swapped and backup is not None:
            from main import manager_command
            restored = manager_command('rollback_update', game=name, backup=str(backup))
            if not restored.get('ok'):
                error += '; AUTOMATIC ROLLBACK FAILED: ' + restored.get('error', 'unknown')
        with LOCK:
            state = json.loads((root / 'state.json').read_text('utf-8'))
            state['status'] = 'failed'
            state['update_error'] = error
            save_state(root, state)
    else:
        with LOCK:
            state = json.loads((root / 'state.json').read_text('utf-8'))
            state['status'] = 'updated'
            state['update_result'] = result
            save_state(root, state)
        # STORYBURROW_STAGE3C_PAYLOAD_PRUNE: preserve update status and snapshot.
        try:
            from staging_cleanup import prune_completed_payload
            prune_completed_payload(root)
        except Exception:
            import logging
            logging.getLogger('storyburrow.update').exception('Could not prune completed update payload')
    finally:
        if pending is not None and pending.exists():
            shutil.rmtree(pending)
        if reserved:
            from main import manager_command
            manager_command('release_update', game=name)
        UPDATE_LOCK.release()


@bp.get('/api/games/<game_id>/update/<token>')
def update_status(game_id, token):
    if not TOKEN.fullmatch(token):
        abort(404)
    with LOCK:
        _, state = get_upload(token)
        return jsonify(ok=True, status=state['status'], error=state.get('update_error'), result=state.get('update_result'))

"""Manage retained previous game builds. Game-file operations are serialized through manager reservation."""
import hmac
import logging
import shutil
import threading
import time
from datetime import datetime
from flask import Blueprint, abort, redirect, render_template, request, session, url_for, flash
from catalog import connect
from build_backups import list_backups, check_owner, remove_backup, expired_tokens, ROOT, MARKER

bp = Blueprint('build_backups', __name__)
LOCK = threading.RLock()


def _guard(name, csrf=False):
    from main import manager_command
    if csrf and not hmac.compare_digest(request.form.get('csrf_token',''), session.get('_csrf','not-set')):
        abort(400)
    status = manager_command('status')
    if not status.get('ok'):
        abort(503, 'Manager unavailable')
    if status.get('running'):
        abort(409, 'Stop the running game first')
    with connect() as db:
        row = db.execute("SELECT * FROM installations WHERE directory_name=? AND status='installed'", (name,)).fetchone()
    if row is None:
        abort(404)
    return dict(row)


def _reserve(game):
    from main import manager_command
    result = manager_command('reserve_update', game=game)
    if not result.get('ok'):
        raise ValueError(result.get('error', 'Manager could not reserve game'))


def _release(game):
    from main import manager_command
    manager_command('release_update', game=game)


@bp.get('/games/<game_id>/previous-builds')
def page(game_id):
    row = _guard(game_id)
    from main import csrf_token
    backups = list_backups(row['installation_id'])
    for backup in backups:
        backup['expiration_local'] = datetime.fromtimestamp(backup['expires_at']).astimezone().strftime('%Y-%m-%d %H:%M %Z')
    return render_template('previous_builds.html', game_id=game_id, backups=backups, csrf=csrf_token())


@bp.post('/games/<game_id>/previous-builds/<token>/delete')
def delete(game_id, token):
    row = _guard(game_id, True)
    if request.form.get('confirm', '').strip().lower() != 'delete':
        abort(400, 'Type delete to confirm')
    with LOCK:
        _reserve(game_id)
        try:
            remove_backup(token, game_id, row['installation_id'])
        finally:
            _release(game_id)
    flash('Previous build deleted.', 'success')
    return redirect(url_for('.page', game_id=game_id), code=303)


@bp.post('/games/<game_id>/previous-builds/<token>/rollback')
def rollback(game_id, token):
    row = _guard(game_id, True)
    if request.form.get('confirm', '').strip().lower() != 'rollback':
        abort(400, 'Type rollback to confirm')
    from main import manager_command
    with LOCK:
        _reserve(game_id)
        try:
            folder, info = check_owner(token, game_id, row['installation_id'])
            from save_routing import _read_config, SAVES
            from pathlib import Path
            route = _read_config().get(game_id)
            if (not route or route.get('namespace') != row['managed_save_namespace'] or
                    not row['managed_save_namespace'] or not folder.joinpath('game','saves').is_symlink() or
                    folder.joinpath('game','saves').resolve() != (SAVES / row['managed_save_namespace'] / 'current').resolve()):
                raise ValueError('Retained build has incompatible managed-save routing')
            outcome = manager_command('rollback_update', game=game_id, backup=str(folder))
            if not outcome.get('ok'):
                raise ValueError(outcome.get('error', 'Rollback failed'))
            failed_build = outcome['failed_build']
            try:
                with connect() as db:
                    cur = db.execute("UPDATE installations SET installed_version=?, launcher_name=? WHERE installation_id=? AND directory_name=? AND status='installed'", (info['previous_version'], info.get('previous_launcher',row['launcher_name']), row['installation_id'], game_id))
                    if cur.rowcount != 1:
                        raise ValueError('Catalog update failed')
                    import json
                    db.execute("UPDATE metadata_fields SET field_value_json=?,source_type='rollback',updated_at=? WHERE game_id=? AND field_name='installed_version'", (json.dumps(info['previous_version']), int(time.time()), row['game_id']))
            except Exception:
                restored = manager_command('rollback_update', game=game_id, backup=failed_build)
                if not restored.get('ok'):
                    logging.getLogger('storyburrow.build-backups').exception('URGENT: rollback catalog failure and restore failed: %s', restored)
                raise
            # The restored build is active again, so its retention marker is no longer valid.
            marker = ROOT.parent / game_id / MARKER
            if marker.is_file() and not marker.is_symlink():
                marker.unlink()
            # Discard the replaced (potentially broken) build only after catalog commit.
            from build_backups import remove_unretained
            remove_unretained(failed_build)
            flash('Previous build restored. Current saves were not changed.', 'success')
        finally:
            _release(game_id)
    return redirect(url_for('edits.edit_game', game_id=game_id), code=303)


LAST_RUN = 0

def cleanup_expired():
    """Best-effort cleanup. Never delete an unmarked legacy backup."""
    global LAST_RUN
    if time.time() - LAST_RUN < 3600:
        return
    if not LOCK.acquire(blocking=False):
        return
    try:
        LAST_RUN = time.time()
        for info in expired_tokens():
            game = info['directory']
            try:
                # Can't reserve an uninstalled game; leave those for manual inspection.
                row = _guard_background(game)
                if row is None or row['installation_id'] != info['installation_id']:
                    continue
                _reserve(game)
                try:
                    remove_backup(info['token'], game, row['installation_id'])
                finally:
                    _release(game)
            except Exception:
                logging.getLogger('storyburrow.build-backups').exception('Could not expire backup %s', info['token'])
    finally:
        LOCK.release()


def _guard_background(name):
    from main import manager_command
    status = manager_command('status')
    if not status.get('ok') or status.get('running'):
        return None
    with connect() as db:
        row = db.execute("SELECT * FROM installations WHERE directory_name=? AND status='installed'", (name,)).fetchone()
    return dict(row) if row else None

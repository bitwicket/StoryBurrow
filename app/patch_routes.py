import hmac
import json
from pathlib import Path
from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for, jsonify
from werkzeug.exceptions import RequestEntityTooLarge
from patcher import PatchError, apply, create_stage, history, preview, revert, _read_stage

bp = Blueprint('patches', __name__)

def _csrf():
    if not hmac.compare_digest(request.form.get('csrf_token', ''), session.get('_csrf', 'not-set')):
        abort(400, description='Invalid form token')

def _game(game_id):
    from main import GAMES, installed_games, manager_command
    status = manager_command('status')
    if not status.get('ok'):
        abort(503, description='Game manager unavailable')
    if status.get('running'):
        abort(409, description='Stop the running game before managing patches')
    games, error = installed_games()
    if error:
        abort(503, description=error)
    if game_id not in {g['id'] for g in games}:
        abort(404)
    directory = GAMES / game_id
    if directory.is_symlink() or directory.resolve().parent != GAMES:
        abort(400, description='Invalid game directory')
    return directory

def _owner():
    return session['user']

def _reserved(game_id, callback):
    from main import manager_command
    result = manager_command('reserve_update', game=game_id)
    if not result.get('ok'):
        raise PatchError(result.get('error', 'Game is busy'))
    try:
        return callback()
    finally:
        manager_command('release_update', game=game_id)

@bp.route('/games/<game_id>/patches', methods=['GET'])
def page(game_id):
    directory = _game(game_id)
    from main import csrf_token
    try:
        records = history(game_id)
    except (PatchError, OSError, ValueError) as exc:
        records = []
        flash(str(exc), 'error')
    return render_template('patch_game.html', game_id=game_id,
                           csrf=csrf_token(), patches=records,
                           preview=None, token=None)

@bp.post('/games/<game_id>/patches/preview')
def upload(game_id):
    _csrf()
    directory = _game(game_id)
    uploaded = request.files.get('patch')
    if uploaded is None or not uploaded.filename:
        abort(400, description='Choose a ZIP or standalone patch file')
    try:
        token, state = create_stage(game_id, _owner(), uploaded, directory,
                                    request.form.get('destination', '').strip() or None)
        rows = preview(directory, state)
        from main import csrf_token
        return render_template('patch_game.html', game_id=game_id, csrf=csrf_token(),
                               patches=history(game_id), preview=rows, token=token,
                               stripped=state.get('stripped'))
    except (PatchError, OSError, ValueError) as exc:
        flash(str(exc), 'error')
        return redirect(url_for('.page', game_id=game_id), code=303)

@bp.post('/games/<game_id>/patches/<token>/apply')
def install(game_id, token):
    _csrf()
    directory = _game(game_id)
    try:
        _read_stage(token, game_id, _owner())
        result = _reserved(game_id, lambda: apply(game_id, _owner(), token, directory))
        flash('Patch installed: ' + result['name'], 'success')
    except (PatchError, OSError, ValueError) as exc:
        flash('Patch not installed: ' + str(exc), 'error')
    return redirect(url_for('.page', game_id=game_id), code=303)

@bp.post('/games/<game_id>/patches/<patch_id>/revert')
def undo(game_id, patch_id):
    _csrf()
    directory = _game(game_id)
    try:
        result = _reserved(game_id, lambda: revert(game_id, patch_id, directory))
        flash('Patch reverted: ' + result['name'], 'success')
    except (PatchError, OSError, ValueError) as exc:
        flash('Revert blocked: ' + str(exc), 'error')
    return redirect(url_for('.page', game_id=game_id), code=303)

@bp.post('/api/games/<game_id>/patches/url')
def url_begin(game_id):
    from import_routes import require_csrf, problem
    from import_url_routes import begin_download
    require_csrf()
    _game(game_id)
    data = request.get_json(silent=True) or {}
    url = data.get('url')
    destination = data.get('destination') or None
    if not isinstance(url, str) or not (1 <= len(url) <= 8192):
        return problem('Provide a valid download URL')
    if destination is not None and (not isinstance(destination, str) or len(destination) > 1024):
        return problem('Invalid standalone file destination')
    try:
        if destination:
            from patcher import _safe_path
            destination = _safe_path(destination)
        token = begin_download(url, _owner(), purpose='patch',
                               game_id=game_id, destination=destination)
    except (PatchError, ValueError) as exc:
        return problem(str(exc))
    return jsonify(ok=True, upload_id=token, status='downloading'), 202

@bp.get('/api/games/<game_id>/patches/url/<token>')
def url_progress(game_id, token):
    from import_routes import LOCK, get_upload
    with LOCK:
        root, state = get_upload(token)
        if state.get('purpose') != 'patch' or state.get('game_id') != game_id:
            abort(404)
        result = dict(ok=True, status=state['status'],
                      received_bytes=state['received'], total_bytes=state['total'],
                      error=state.get('error'))
        if state['status'] == 'ready':
            result.update(token=state['patch_token'],
                          preview=state['patch_preview'], stripped=state.get('stripped'))
    return jsonify(result)

@bp.post('/api/games/<game_id>/patches/url/<token>/cancel')
def url_cancel(game_id, token):
    from import_routes import LOCK, get_upload, require_csrf, save_state
    require_csrf()
    with LOCK:
        root, state = get_upload(token)
        if state.get('purpose') != 'patch' or state.get('game_id') != game_id:
            abort(404)
        if state['status'] in ('downloading', 'inspecting'):
            state['cancel_requested'] = True
            save_state(root, state)
    return jsonify(ok=True)

@bp.get('/games/<game_id>/patches/preview/<token>')
def show_url_preview(game_id, token):
    directory = _game(game_id)
    try:
        _, state = _read_stage(token, game_id, _owner())
        from main import csrf_token
        return render_template('patch_game.html', game_id=game_id,
                               csrf=csrf_token(), patches=history(game_id),
                               preview=preview(directory, state), token=token,
                               stripped=state.get('stripped'))
    except (PatchError, OSError, ValueError) as exc:
        flash(str(exc), 'error')
        return redirect(url_for('.page', game_id=game_id), code=303)



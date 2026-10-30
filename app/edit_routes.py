import hmac
import shutil
import threading
import logging
from pathlib import Path
from flask import Blueprint, abort, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from artwork import ArtworkError
from art_picker import choices, explicit_allowed, set_explicit_allowed, save_choice
from catalog import installed_by_directory, update_game_details, remove_managed_installation

bp = Blueprint('edits', __name__)


def _context(directory_name):
    # Imported lazily to avoid circular imports: main.py registers this blueprint.
    from main import installed_games, manager_command
    status = manager_command('status')
    if not status.get('ok'):
        abort(503, description='Game manager unavailable; editing is disabled')
    if status.get('running'):
        abort(409, description='Stop the current game before editing library entries')
    games, error = installed_games()
    if error:
        abort(503, description=error)
    game = next((g for g in games if g['id'] == directory_name), None)
    if game is None:
        abort(404)
    record = installed_by_directory().get(directory_name)
    return game, record


@bp.route('/games/<game_id>/edit', methods=['GET', 'POST'])
def edit_game(game_id):
    game, record = _context(game_id)
    error = None
    if request.method == 'POST':
        token = request.form.get('csrf_token', '')
        if not hmac.compare_digest(token, session.get('_csrf', 'not-set')):
            abort(400, description='Invalid form token')
        if record is None:
            abort(403, description='Manually added games are read-only until adopted by the catalog')
        title = request.form.get('title', '')
        version = request.form.get('version', '')
        try:
            update_game_details(game_id, title, version)
        except ValueError as exc:
            error = str(exc)
        else:
            return redirect(url_for('library'), code=303)
        game['title'] = title
        game['version'] = version
    from main import csrf_token
    return render_template('edit_game.html', game=game, record=record,
                           csrf=csrf_token(), error=error)


@bp.errorhandler(HTTPException)
def edit_error(error):
    titles = {409: 'Editing unavailable', 503: 'Game manager unavailable',
              404: 'Game not found', 400: 'Invalid request', 403: 'Editing unavailable'}
    if request.path.endswith('/artwork') or request.path.endswith('/artwork/preferences'):
        return {'ok': False, 'error': error.description}, error.code
    return render_template('message.html', title=titles.get(error.code, error.name),
                           message=error.description), error.code


def _check_csrf():
    token = request.headers.get('X-CSRF-Token', '')
    if not hmac.compare_digest(token, session.get('_csrf', 'not-set')):
        abort(400, description='Invalid form token')


@bp.get('/games/<game_id>/artwork')
def artwork_options(game_id):
    _, record = _context(game_id)
    if not record:
        abort(403, description='Only catalog-managed games can select VNDB artwork')
    try:
        images = choices(record['vndb_id']) if record.get('vndb_id') else []
        return {'ok': True, 'images': images, 'allow_explicit': explicit_allowed()}
    except (ArtworkError, OSError, ValueError) as exc:
        return {'ok': False, 'error': str(exc)}, 502


@bp.post('/games/<game_id>/artwork/preferences')
def artwork_preferences(game_id):
    _check_csrf()
    _, record = _context(game_id)
    if not record:
        abort(403)
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get('allow_explicit'), bool):
        abort(400, description='Expected a boolean preference')
    set_explicit_allowed(body['allow_explicit'])
    return {'ok': True, 'allow_explicit': explicit_allowed()}


@bp.post('/games/<game_id>/artwork')
def artwork_save(game_id):
    _check_csrf()
    _, record = _context(game_id)
    if not record:
        abort(403)
    data = request.get_json(silent=True) or {}
    image_id = data.get('image_id')
    if not isinstance(image_id, str) or len(image_id) > 64:
        abort(400, description='Invalid image selection')
    try:
        save_choice(record['game_id'], image_id)
        return {'ok': True}
    except (ArtworkError, OSError, ValueError) as exc:
        return {'ok': False, 'error': str(exc)}, 422


@bp.post('/games/<game_id>/uninstall')
def uninstall_game(game_id):
    # Form POST only; validate CSRF and catalog before contacting the manager.
    token = request.form.get('csrf_token', '')
    if not hmac.compare_digest(token, session.get('_csrf', 'not-set')):
        abort(400, description='Invalid form token')
    game, record = _context(game_id)
    if record is None:
        abort(403, description='Only catalog-managed installations can be uninstalled')
    if request.form.get('confirm_directory') != game_id:
        abort(400, description='Installation directory confirmation did not match')
    from main import manager_command
    from managed_save_safety import verify_uninstall
    try:
        verify_uninstall(game_id)
    except (ValueError, OSError, KeyError) as exc:
        abort(409, description=str(exc))
    retired = manager_command('retire', game=game_id)
    if not retired.get('ok'):
        abort(409, description=retired.get('error', 'Could not safely retire installation'))
    trash_token = retired.get('trash_token', '')
    if not isinstance(trash_token, str) or len(trash_token) != 32:
        abort(503, description='Game manager returned an invalid uninstall token')
    try:
        remove_managed_installation(game_id, record['game_id'])
    except Exception:
        restored = manager_command('restore', game=game_id, trash_token=trash_token)
        logging.getLogger('storyburrow.uninstall').exception(
            'Catalog update failed; restore result: %s', restored)
        abort(503, description='Uninstall could not update catalog. Check server logs before retrying.')
    try:
        from save_routing import retire_uninstalled_route
        retire_uninstalled_route(game_id)
    except Exception:
        logging.getLogger('storyburrow.uninstall').exception(
            'Could not retire save route for %s; reinstall will recheck it', game_id)
    trash = Path('/games/.storyburrow-uninstall-trash') / trash_token
    def cleanup_retired():
        try:
            if trash.parent.is_symlink() or trash.is_symlink():
                raise RuntimeError('Refusing symlinked uninstall trash')
            shutil.rmtree(trash)
        except Exception:
            logging.getLogger('storyburrow.uninstall').exception(
                'Failed cleaning retired game at %s; manual cleanup may be needed', trash)
    threading.Thread(target=cleanup_retired, name='storyburrow-uninstall-cleanup', daemon=True).start()
    return redirect(url_for('library'), code=303)

@bp.post('/games/<game_id>/forget')
def forget_game(game_id):
    token = request.form.get('csrf_token', '')
    if not hmac.compare_digest(token, session.get('_csrf', 'not-set')):
        abort(400, description='Invalid form token')

    game, record = _context(game_id)
    if record is None:
        abort(403, description='Only catalog-managed installations can be forgotten')

    if request.form.get('confirm_delete') != 'delete':
        abort(400, description='Type delete to confirm complete removal')

    from main import manager_command
    from managed_save_safety import verify_uninstall
    from forget_cleanup import preflight, cleanup

    try:
        verify_uninstall(game_id)
        ownership = preflight(game_id, record['game_id'])
    except (ValueError, OSError, KeyError) as exc:
        abort(409, description=str(exc))

    retired = manager_command('retire', game=game_id)
    if not retired.get('ok'):
        abort(409, description=retired.get('error', 'Could not retire installation'))

    trash_token = retired.get('trash_token', '')
    if not isinstance(trash_token, str) or len(trash_token) != 32:
        abort(503, description='Invalid uninstall token returned by manager')

    try:
        remove_managed_installation(game_id, record['game_id'])
    except Exception:
        restored = manager_command(
            'restore', game=game_id, trash_token=trash_token
        )
        logging.getLogger('storyburrow.forget').exception(
            'Catalog removal failed; restore result: %s', restored
        )
        abort(503, description='Could not remove installation from catalog')

    try:
        from save_routing import retire_uninstalled_route
        retire_uninstalled_route(game_id)
    except Exception:
        logging.getLogger('storyburrow.forget').exception(
            'Failed retiring save route for %s', game_id
        )
        abort(503, description=(
            'Installation was removed, but its save route could not be retired. '
            'Managed saves were preserved. Check server logs.'
        ))

    try:
        cleanup(ownership)
    except Exception:
        logging.getLogger('storyburrow.forget').exception(
            'Complete removal cleanup failed for %s', game_id
        )
        abort(503, description=(
            'Installation was removed, but some managed data may remain. '
            'Check server logs before retrying.'
        ))

    trash = Path('/games/.storyburrow-uninstall-trash') / trash_token

    def cleanup_retired():
        try:
            if trash.parent.is_symlink() or trash.is_symlink():
                raise RuntimeError('Refusing symlinked uninstall trash')
            shutil.rmtree(trash)
        except Exception:
            logging.getLogger('storyburrow.forget').exception(
                'Failed cleaning retired installation at %s', trash
            )

    threading.Thread(
        target=cleanup_retired,
        name='storyburrow-forget-cleanup',
        daemon=True,
    ).start()

    return redirect(url_for('library'), code=303)
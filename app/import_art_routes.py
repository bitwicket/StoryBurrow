from flask import Blueprint, request, abort, session
import hmac
from artwork import ArtworkError
from art_picker import choices, explicit_allowed, set_explicit_allowed

bp = Blueprint('import_art', __name__)

@bp.get('/api/import/artwork')
def artwork_options():
    vndb_id = request.args.get('vndb_id', '')
    try:
        return {'ok': True, 'images': choices(vndb_id), 'allow_explicit': explicit_allowed()}
    except (ArtworkError, OSError, ValueError) as exc:
        return {'ok': False, 'error': str(exc)}, 502

@bp.post('/api/import/artwork/preferences')
def artwork_preferences():
    if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), session.get('_csrf', 'not-set')):
        abort(400, description='Invalid CSRF token')
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('allow_explicit'), bool):
        return {'ok': False, 'error': 'Expected a boolean preference'}, 400
    set_explicit_allowed(data['allow_explicit'])
    return {'ok': True, 'allow_explicit': explicit_allowed()}

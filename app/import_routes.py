import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request, session
from importer.inspector import InspectionError, inspect, normalize_path
from importer.vndb import VNDBError, search_vn
from catalog import initialize as init_catalog, save_review, delete_review, get_review, register_installation
from installer import prepare, target_name, InstallError, GAMES
from save_inspector import inspect_save_directory

bp = Blueprint('imports', __name__)
ROOT = Path('/data/import-staging')
CHUNK = 16 * 1024**2
MAX_CHUNK_REQUEST = 20 * 1024**2
MAX_UPLOAD = 20 * 1024**3  # combined file size; change independently of HTTP limit
MAX_FILES = 20000
MAX_PART = 20 * 1024**3
MAX_AGE = 24 * 3600
LOCK = threading.RLock()  # initial implementation: one worker per process
TOKEN = re.compile(r'^[a-f0-9]{32}$')


def require_csrf():
    if not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), session.get('_csrf', 'not-set')):
        abort(400, 'Invalid CSRF token')


def problem(message, code=400):
    return jsonify(ok=False, error=message), code


def cleanup():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = time.time()
    for p in ROOT.iterdir():
        if p.is_dir() and TOKEN.fullmatch(p.name):
            try:
                if now - p.stat().st_mtime > MAX_AGE:
                    shutil.rmtree(p)
            except OSError:
                current_app.logger.exception('Could not remove expired import preview')


def get_upload(token):
    if not TOKEN.fullmatch(token):
        abort(404)
    root = ROOT / token
    try:
        data = json.loads((root / 'state.json').read_text('utf-8'))
    except (OSError, ValueError):
        abort(404)
    if data.get('owner') != session.get('user'):
        abort(404)
    return root, data


def save_state(root, state):
    tmp = root / 'state.tmp'
    tmp.write_text(json.dumps(state, separators=(',', ':')), encoding='utf-8')
    os.replace(tmp, root / 'state.json')
    root.touch()


def public_state(token, state):
    return {'upload_id': token, 'kind': state['kind'], 'received_bytes': state['received'],
            'total_bytes': state['total'], 'offsets': state['offsets'], 'status': state['status']}


@bp.post('/api/import/uploads')
def upload_create():
    require_csrf()
    data = request.get_json(silent=True) or {}
    kind, files = data.get('kind'), data.get('files')
    if kind not in ('zip', 'folder') or not isinstance(files, list):
        return problem('Invalid upload manifest')
    if not (1 <= len(files) <= MAX_FILES) or (kind == 'zip' and len(files) != 1):
        return problem('Unsupported file count')
    validated, seen, total = [], set(), 0
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
            return problem('Invalid file manifest')
        try:
            path = normalize_path(entry['path'])
        except InspectionError as exc:
            return problem(str(exc))
        if path in seen or (kind == 'zip' and not path.lower().endswith('.zip')):
            return problem('Duplicate path or unsupported ZIP filename')
        seen.add(path)
        size = entry.get('size')
        if type(size) is not int or size < 0 or size > MAX_PART:
            return problem('Invalid file size')
        total += size
        if total > MAX_UPLOAD:
            return problem('Upload exceeds total size limit', 413)
        validated.append({'path': path, 'size': size})
    with LOCK:
        cleanup()
        token = secrets.token_hex(16)
        root = ROOT / token
        root.mkdir(mode=0o700)
        (root / 'parts').mkdir()
        state = {'owner': session['user'], 'kind': kind, 'files': validated, 'total': total,
                 'offsets': [0] * len(files), 'received': 0, 'status': 'uploading'}
        save_state(root, state)
    return jsonify(ok=True, **public_state(token, state))


@bp.get('/api/import/uploads/<token>')
def upload_status(token):
    with LOCK:
        _, state = get_upload(token)
        return jsonify(ok=True, **public_state(token, state))


@bp.put('/api/import/uploads/<token>/chunk/<int:index>')
def upload_chunk(token, index):
    require_csrf()
    # request.max_content_length is also set centrally in main.py's before_request.
    request.max_content_length = MAX_CHUNK_REQUEST
    with LOCK:
        root, state = get_upload(token)
        if state['status'] != 'uploading' or not (0 <= index < len(state['files'])):
            return problem('Invalid upload state or file index', 409)
        try:
            offset = int(request.headers['X-Chunk-Offset'])
        except (KeyError, ValueError):
            return problem('Missing or invalid chunk offset')
        expected = state['offsets'][index]
        if offset != expected:
            return jsonify(ok=False, error='Offset mismatch', expected_offset=expected), 409
        remaining = state['files'][index]['size'] - expected
        if remaining <= 0:
            return problem('File already complete', 409)
        body = request.get_data(cache=False)
        if not body or len(body) > CHUNK or len(body) > remaining:
            return problem('Invalid chunk size', 413)
        dest = root / 'parts' / str(index)
        # Store chunks in one file per manifest entry; no browser-supplied disk paths.
        with dest.open('ab') as f:
            if f.tell() != expected:
                return problem('Staged file size mismatch', 409)
            f.write(body)
            f.flush()
        state['offsets'][index] += len(body)
        state['received'] += len(body)
        save_state(root, state)
        return jsonify(ok=True, received_bytes=state['received'], offset=state['offsets'][index])


@bp.post('/api/import/uploads/<token>/complete')
def upload_complete(token):
    require_csrf()
    with LOCK:
        root, state = get_upload(token)
        if state['status'] == 'ready' and (root / 'inspection.json').exists():
            return jsonify(ok=True, inspection=json.loads((root / 'inspection.json').read_text('utf-8')))
        if state['status'] != 'uploading' or state['offsets'] != [f['size'] for f in state['files']]:
            return problem('Not all files have been uploaded', 409)
        try:
            if state['kind'] == 'zip':
                source = root / 'upload.zip'
                os.replace(root / 'parts' / '0', source)
            else:
                source = root / 'folder'
                source.mkdir(exist_ok=True)
                for i, item in enumerate(state['files']):
                    dest = source / item['path']
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    part = root / 'parts' / str(i)
                    if item['size'] == 0:
                        dest.touch(exist_ok=True)
                    else:
                        os.replace(part, dest)
            inspected = inspect(source)
            inspected['source']['name'] = (state['files'][0]['path'].split('/')[0]
                                            if state['kind'] == 'folder' else Path(state['files'][0]['path']).name)
            inspected['preview_id'] = token
            (root / 'inspection.json').write_text(json.dumps(inspected), encoding='utf-8')
            state['status'] = 'ready'
            save_state(root, state)
            return jsonify(ok=True, inspection=inspected)
        except (InspectionError, OSError, ValueError) as exc:
            # Keep files for error diagnosis/retry; expire by age.
            current_app.logger.warning('Import inspection failed: %s', exc)
            return problem(str(exc))


@bp.delete('/api/import/uploads/<token>')
def upload_delete(token):
    require_csrf()
    with LOCK:
        root, state = get_upload(token)
        if state['status'] in ('installing', 'installed'):
            return problem('Cannot discard installation in progress', 409)
        shutil.rmtree(root, ignore_errors=True)
        delete_review(token, session['user'])
    return jsonify(ok=True)


@bp.post('/api/import/vndb')
def import_vndb():
    require_csrf()
    data = request.get_json(silent=True) or {}
    title = data.get('title', '')
    if not isinstance(title, str):
        return problem('Invalid title')
    try:
        return jsonify(ok=True, candidates=search_vn(title, limit=8))
    except VNDBError as exc:
        return problem(str(exc), 502)

@bp.post('/api/import/discard')
def import_discard():
    require_csrf()
    data = request.get_json(silent=True) or {}
    token = data.get('preview_id', '')
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        return problem('Invalid preview ID')
    with LOCK:
        root, state = get_upload(token)
        if state['status'] in ('installing', 'installed'):
            return problem('Cannot discard installation in progress', 409)
        shutil.rmtree(root, ignore_errors=True)
        delete_review(token, session['user'])
    return jsonify(ok=True)


@bp.post('/api/import/uploads/<token>/review')
def upload_review(token):
    require_csrf()
    with LOCK:
        root, state = get_upload(token)
        if state['status'] != 'ready':
            return problem('Inspect the upload before saving a review', 409)
        try:
            original = json.loads((root / 'inspection.json').read_text('utf-8'))
        except (OSError, ValueError):
            return problem('Inspection result unavailable', 409)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return problem('Invalid review data')
        title = payload.get('title')
        version = payload.get('installed_version', '')
        candidate = payload.get('vndb')
        if candidate is not None and not isinstance(candidate, dict):
            return problem('Invalid VNDB selection')
        if candidate is not None and len(json.dumps(candidate)) > 32768:
            return problem('VNDB selection too large', 413)
        vndb_id = candidate.get('id') if candidate is not None else None
        try:
            init_catalog()
            saved = save_review(token, session['user'], title, version, vndb_id, candidate, original)
        except (ValueError, PermissionError) as exc:
            return problem(str(exc))
        image_id = payload.get('artwork_image_id')
        if image_id is not None:
            from art_picker import choices
            from artwork import ArtworkError
            if not vndb_id or not isinstance(image_id, str) or len(image_id) > 64:
                return problem('Invalid artwork selection')
            try:
                if image_id not in {item['id'] for item in choices(vndb_id)}:
                    return problem('Artwork is unavailable or disallowed by content settings', 422)
            except (ArtworkError, OSError, ValueError) as exc:
                return problem(f'Could not verify artwork: {exc}', 502)
        state['artwork_image_id'] = image_id
        save_state(root, state)
        return jsonify(ok=True, review=saved, installation_enabled=False)


@bp.post('/api/import/uploads/<token>/install')
def install_game(token):
    require_csrf()
    with LOCK:
        root, state = get_upload(token)
        if state['status'] != 'ready':
            return problem('Upload must be inspected before installation', 409)
        init_catalog()
        review = get_review(token, session['user'])
        if not review:
            return problem('Save your review before installing', 409)
        folder_name = target_name(review['title'])
        if (GAMES / folder_name).exists():
            return problem('A game installation already uses this directory name', 409)
        state['status'] = 'installing'
        state['install_error'] = None
        state['install_result'] = None
        save_state(root, state)
        # Keep Flask responsive for multi-gigabyte extraction.
        threading.Thread(target=_install_worker,
                         args=(token, session['user'], folder_name), daemon=True).start()
        return jsonify(ok=True, status='installing', upload_id=token), 202


def _install_worker(token, owner, folder_name):
    pending = None
    destination = GAMES / folder_name
    published = False
    registered = False
    routed = False
    namespace = None
    root = ROOT / token
    try:
        state = json.loads((root / 'state.json').read_text('utf-8'))
        if state['owner'] != owner or state['status'] != 'installing':
            raise InstallError('Upload session changed during installation')
        source = root / ('upload.zip' if state['kind'] == 'zip' else 'folder')
        if not source.exists():
            raise InstallError('Staged upload was not found')
        pending, launcher, fresh = prepare(source, folder_name)
        original = json.loads((root / 'inspection.json').read_text('utf-8'))
        if (fresh['detected']['launcher'] != original['detected']['launcher'] or
                fresh['detected']['root_prefix'] != original['detected']['root_prefix']):
            raise InstallError('Game structure differs from review')
        save_identity = inspect_save_directory(pending)
        with LOCK:
            state = json.loads((root / 'state.json').read_text('utf-8'))
            if state['status'] != 'installing' or get_review(token, owner) is None:
                raise InstallError('Review missing during installation')
            if destination.exists():
                raise InstallError('Destination was created during installation')
            # STORYBURROW_STAGE3B_AUTO: every new import gets a unique save namespace.
            import uuid
            from save_routing import prepare as prepare_saves
            installation_id = str(uuid.uuid4())
            namespace = f'{folder_name}-{installation_id}'
            # Clear a verified orphan before publishing this new directory.
            # This operation never touches /saves or the previous namespace.
            from save_routing import retire_orphaned_route
            retire_orphaned_route(folder_name, expected_directory=destination)
            os.rename(pending, destination)
            pending = None
            published = True
            try:
                save_id = save_identity.get('save_directory')
            except (NameError, AttributeError):
                save_id = None
            prepare_saves(folder_name, namespace, save_id=save_id)
            routed = True
            result = register_installation(token, owner, folder_name, launcher,
                                           state['kind'] + '_upload', state['files'][0]['path'],
                                           installation_id=installation_id, managed_save_namespace=namespace)
            registered = True
            try:
                from catalog import update_installation_save_directory
                update_installation_save_directory(folder_name, save_identity)
            except Exception:
                import logging
                logging.getLogger('storyburrow.import').exception(
                    'Could not persist installation save identifier')
            state['status'] = 'installing'  # Wait for artwork before reporting completion.
            state['install_result'] = result
            save_state(root, state)
        artwork_error = None
        try:
            from artwork import cache_vndb_cover
            selected_image_id = state.get('artwork_image_id')
            if selected_image_id:
                from art_picker import save_choice
                save_choice(result['game_id'], selected_image_id)
            else:
                cache_vndb_cover(result['game_id'])
        except Exception as exc:
            artwork_error = str(exc)[:300]
            import logging
            logging.getLogger('storyburrow.artwork').exception('Could not cache VNDB cover')
        with LOCK:
            state = json.loads((root / 'state.json').read_text('utf-8'))
            state['status'] = 'installed'
            state['artwork_error'] = artwork_error
            save_state(root, state)
        # STORYBURROW_STAGE3C_PAYLOAD_PRUNE: keep small status for browser polling.
        try:
            from staging_cleanup import prune_completed_payload
            prune_completed_payload(root)
        except Exception:
            import logging
            logging.getLogger('storyburrow.import').exception('Could not prune completed import payload')
    except Exception as exc:
        if published and not registered:
            # Undo only the unpublished routing entry. Never delete its save namespace.
            if routed:
                try:
                    from save_routing import _read_config, _write_config
                    config = _read_config()
                    if config.get(folder_name, {}).get('namespace') == namespace:
                        del config[folder_name]
                        _write_config(config)
                except Exception:
                    import logging
                    logging.getLogger('storyburrow.import').exception(
                        'Could not clear failed routing registration')

            try:
                shutil.rmtree(destination)
            except OSError:
                pass
        with LOCK:
            try:
                state = json.loads((root / 'state.json').read_text('utf-8'))
                state['status'] = 'installed' if registered else 'failed'
                state['install_error'] = 'Catalog registered; refresh library' if registered else str(exc)
                save_state(root, state)
            except (OSError, ValueError):
                pass
    finally:
        if pending is not None:
            shutil.rmtree(pending, ignore_errors=True)


@bp.get('/api/import/uploads/<token>/installation')
def installation_status(token):
    with LOCK:
        root, state = get_upload(token)
        return jsonify(ok=True, status=state['status'], error=state.get('install_error'),
                       installation=state.get('install_result'),
                       artwork_error=state.get('artwork_error'))

import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

from flask import Blueprint, abort, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from catalog import connect, initialize
from save_inspector import inspect_save_directory
from save_routing import GAMES, SAVES, _read_config, _write_config

bp = Blueprint('adoption', __name__)
VALID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$')
LOCK_PATH = Path('/data/adoption.lock')


def _manager_stopped():
    from main import manager_command
    status = manager_command('status')
    if not status.get('ok'):
        abort(503, description='Game manager unavailable; adoption is disabled')
    if status.get('running'):
        abort(409, description='Stop the running game before adopting saves')


@contextmanager
def _lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open('a+b') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _files(path):
    if not path.exists():
        return {}

    if path.is_symlink() or not path.is_dir():
        raise ValueError(f'Legacy save directory is unsafe: {path}')

    found = {}

    for item in path.iterdir():
        if item.is_symlink():
            raise ValueError(f'Symlink inside legacy saves: {item}')

        if item.is_dir():
            # Ren'Py synchronization data is not a save slot.
            # Leave it in the original location; do not merge it
            # into the managed current/ directory.
            if item.name == 'sync':
                continue

            raise ValueError(
                f'Unexpected nested legacy directory: {item}'
            )

        if not item.is_file():
            raise ValueError(f'Unexpected legacy save entry: {item}')

        found[item.name] = item

    return found


def _digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _plan(name):
    if not VALID.fullmatch(name) or name in ('.', '..') or name.startswith('.'):
        abort(404)
    directory = GAMES / name
    if directory.is_symlink() or not directory.is_dir() or not (directory / 'game').is_dir():
        abort(404)
    if directory.resolve().parent != GAMES:
        abort(404)
    if name in _read_config():
        abort(409, description='Managed save routing already exists')
    initialize()
    with connect() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM installations WHERE directory_name=?', (name,))]
    if len(rows) > 1 or (rows and (rows[0]['status'] != 'installed' or rows[0]['managed_save_namespace'])):
        abort(409, description='Catalog installation is not eligible for adoption')
    result = inspect_save_directory(directory)
    save_id = result.get('save_directory') or (rows[0].get('save_directory') if rows else None)
    if not save_id or not VALID.fullmatch(save_id) or save_id in ('.', '..'):
        abort(409, description='Native RenPy save identifier could not be confirmed')
    local = directory / 'game' / 'saves'
    original = directory / 'game' / 'saves.storyburrow-original'
    if original.exists() or original.is_symlink() or local.is_symlink():
        abort(409, description='Legacy local saves already have backup or symlink; manual review required')
    native = SAVES / save_id
    if native.resolve().parent != SAVES or native == local:
        abort(409, description='Unsafe native save path')
    sources = {'native': _files(native), 'local': _files(local)}
    all_names = sorted(set(sources['native']) | set(sources['local']))
    conflicts = [key for key in all_names if key in sources['native'] and key in sources['local'] and _digest(sources['native'][key]) != _digest(sources['local'][key])]
    launchers = sorted(p for p in directory.glob('*.sh') if p.is_file() and not p.is_symlink())
    preferred = directory / f'{name}.sh'
    launcher = preferred if preferred in launchers else launchers[0] if len(launchers) == 1 else None
    if launcher is None:
        abort(409, description='Cannot identify one Linux launcher')
    return {'name': name, 'directory': directory, 'rows': rows, 'save_id': save_id,
            'sources': sources, 'names': all_names, 'conflicts': conflicts,
            'launcher': launcher.name, 'native': native, 'local': local}


def _adopt(p, choices, title, version):
    from main import manager_command
    _manager_stopped()
    existing = p['rows'][0] if p['rows'] else None
    installation_id = existing['installation_id'] if existing else str(uuid.uuid4())
    game_id = existing['game_id'] if existing else str(uuid.uuid4())
    name = p['name']
    namespace = f'{name}-{installation_id}'
    root = SAVES / namespace
    if root.exists() or root.is_symlink():
        raise ValueError('Managed namespace already exists')
    if any(v.get('namespace') == namespace for v in _read_config().values()):
        raise ValueError('Namespace is already assigned')
    current = root / 'current'
    parent = root / 'renpy-parent'
    snap = root / 'snapshots' / ('pre-adoption_' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '_' + uuid.uuid4().hex[:8])
    original_local = p['directory'] / 'game' / 'saves.storyburrow-original'
    moved = False
    linked = False
    configured = False
    catalog_committed = False
    try:
        current.mkdir(parents=True)
        parent.mkdir()
        snap.mkdir(parents=True)
        manifest = {'game': name, 'save_id': p['save_id'], 'choices': choices, 'files': {}}
        for label, files in p['sources'].items():
            (snap / label).mkdir()
            for filename, src in files.items():
                dst = snap / label / filename
                shutil.copy2(src, dst)
                digest = _digest(src)
                if _digest(dst) != digest:
                    raise ValueError(f'Original copy verification failed: {filename}')
                manifest['files'][f'{label}/{filename}'] = digest
        for filename in p['names']:
            selected = choices.get(filename, 'native' if filename in p['sources']['native'] else 'local')
            src = p['sources'][selected][filename]
            shutil.copy2(src, current / filename)
            if _digest(current / filename) != manifest['files'][f'{selected}/{filename}']:
                raise ValueError(f'Active copy verification failed: {filename}')
        (snap / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        (parent / p['save_id']).symlink_to(current, target_is_directory=True)
        _manager_stopped()
        # Native original remains untouched. Local original is retained under the game directory.
        if p['local'].exists():
            p['local'].rename(original_local)
            moved = True
        p['local'].symlink_to(current, target_is_directory=True)
        linked = True
        config = _read_config()
        if name in config:
            raise ValueError('Routing appeared during adoption')
        config[name] = {'namespace': namespace, 'save_id': p['save_id'], 'parent': str(parent)}
        _write_config(config)
        configured = True
        now = int(time.time())
        with connect() as db:
            if existing:
                cursor = db.execute('''UPDATE installations SET managed_save_namespace=?, save_mode='managed',
                    save_directory=?, save_directory_source='adoption', save_directory_confidence='confirmed'
                    WHERE directory_name=? AND installation_id=? AND status='installed' AND managed_save_namespace IS NULL''',
                    (namespace, p['save_id'], name, installation_id))
                if cursor.rowcount != 1:
                    raise ValueError('Catalog installation changed during adoption')
            else:
                if db.execute('SELECT 1 FROM installations WHERE directory_name=?', (name,)).fetchone():
                    raise ValueError('Catalog entry appeared during adoption')
                db.execute('INSERT INTO games (game_id,display_title,description,vndb_id,created_at,updated_at) VALUES (?,?,?,?,?,?)',
                           (game_id, title, None, None, now, now))
                db.execute('''INSERT INTO installations
                    (installation_id,game_id,directory_name,launcher_name,installed_version,source_type,
                     source_filename,source_url,installed_at,status,managed_save_namespace,save_mode,
                     save_directory,save_directory_source,save_directory_confidence)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (installation_id, game_id, name, p['launcher'], version, 'legacy_adoption', None,
                     None, now, 'installed', namespace, 'managed', p['save_id'], 'adoption', 'confirmed'))
        catalog_committed = True
        return namespace
    except Exception:
        if not catalog_committed:
            # Only roll back routing when it still points at our namespace.
            if configured:
                cfg = _read_config()
                if cfg.get(name, {}).get('namespace') == namespace:
                    del cfg[name]
                    _write_config(cfg)
            if linked and p['local'].is_symlink() and p['local'].resolve() == current.resolve():
                p['local'].unlink()
            if moved and original_local.is_dir() and not p['local'].exists():
                original_local.rename(p['local'])
            # Preserve copied data for manual recovery after any failure; do not delete root.
        raise


@bp.route('/games/<game_id>/adopt', methods=['GET', 'POST'])
def adopt(game_id):
    with _lock():
        _manager_stopped()
        p = _plan(game_id)
        if request.method == 'POST':
            if not hmac.compare_digest(request.form.get('csrf_token', ''), session.get('_csrf', 'not-set')):
                abort(400, description='Invalid form token')
            title = request.form.get('title', '').strip()
            version = request.form.get('version', '').strip()
            if not title or len(title) > 200 or len(version) > 100:
                abort(400, description='Invalid title or version')
            if request.form.get('confirm') != game_id:
                abort(400, description='Directory confirmation does not match')
            choices = {}
            for filename in p['conflicts']:
                choice = request.form.get('choose_' + filename)
                if choice not in ('native', 'local'):
                    abort(400, description=f'Choose a source for {filename}')
                choices[filename] = choice
            try:
                _adopt(p, choices, title, version)
            except (OSError, ValueError) as exc:
                abort(409, description=f'Adoption stopped; original saves preserved. {exc}')
            return redirect(url_for('edits.edit_game', game_id=game_id), code=303)
    from main import csrf_token
    return render_template('adopt_game.html', p=p, csrf=csrf_token())


@bp.errorhandler(HTTPException)
def adoption_error(err):
    return render_template('message.html', title='Adoption unavailable', message=err.description), err.code

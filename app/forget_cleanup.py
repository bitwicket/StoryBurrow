import logging
import shutil
from pathlib import Path

LOG = logging.getLogger('storyburrow.forget')


def preflight(game_id, expected_game_id):
    from catalog import connect
    from save_routing import SAVES, _read_config, _valid
    with connect() as db:
        rows = db.execute("SELECT * FROM installations WHERE directory_name=? AND status='installed'", (game_id,)).fetchall()
        if len(rows) != 1 or rows[0]['game_id'] != expected_game_id:
            raise ValueError('Installation identity could not be verified')
        row = dict(rows[0])
        ns = row.get('managed_save_namespace')
        if row.get('save_mode') != 'managed' or not ns:
            raise ValueError('Complete removal requires a managed save installation')
        _valid(ns)
        references = db.execute('SELECT installation_id FROM installations WHERE managed_save_namespace=?', (ns,)).fetchall()
        if len(references) != 1 or references[0]['installation_id'] != row['installation_id']:
            raise ValueError('Managed save namespace is shared by another installation')
    config = _read_config()
    if config.get(game_id, {}).get('namespace') != ns:
        raise ValueError('Managed save route does not match installation')
    if any(name != game_id and isinstance(info, dict) and info.get('namespace') == ns for name, info in config.items()):
        raise ValueError('Another game route references these saves')
    if SAVES.is_symlink() or not SAVES.is_dir():
        raise ValueError('Managed saves root is unavailable or unsafe')
    folder = SAVES / ns
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != SAVES:
        raise ValueError('Managed save namespace is unavailable or unsafe')
    return {'installation_id': row['installation_id'], 'namespace': ns, 'directory': game_id}


def cleanup(ownership):
    from catalog import connect
    from save_routing import SAVES, _read_config
    from build_backups import list_backups, remove_backup
    ns = ownership['namespace']
    name = ownership['directory']
    installation_id = ownership['installation_id']
    with connect() as db:
        if db.execute('SELECT COUNT(*) FROM installations WHERE managed_save_namespace=?', (ns,)).fetchone()[0]:
            raise ValueError('Save namespace was re-registered; refusing cleanup')
    if any(isinstance(route, dict) and route.get('namespace') == ns for route in _read_config().values()):
        raise ValueError('Managed save route still references namespace; refusing cleanup')
    path = SAVES / ns
    if SAVES.is_symlink() or path.is_symlink() or path.resolve().parent != SAVES:
        raise ValueError('Refusing unsafe managed-save cleanup path')
    if path.is_dir():
        shutil.rmtree(path)
    # Ignore legacy unmarked directories; never guess their ownership.
    failed = []
    for info in list_backups(installation_id):
        if info.get('directory') != name:
            continue
        try:
            remove_backup(info['token'], name, installation_id)
        except (ValueError, OSError) as exc:
            LOG.exception('Unable to remove registered previous build %s', info['token'])
            failed.append(info['token'])
    if failed:
        raise OSError('Some previous builds could not be deleted: ' + ', '.join(failed))

"""Previous game builds retained for at most 48 hours; existing unmarked builds are ignored."""
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from catalog import connect

ROOT = Path(os.environ.get('STORYBURROW_GAMES_DIR', '/games')).resolve() / '.storyburrow-update-backups'
MARKER = '.storyburrow-build-retention.json'
LOG = logging.getLogger('storyburrow.build-backups')
TOKEN = re.compile(r'^[0-9a-f]{32}$')
LIFETIME = 48 * 3600


def _folder(token):
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ValueError('Invalid backup identifier')
    if ROOT.is_symlink():
        raise ValueError('Backup root is a symlink')
    folder = ROOT / token
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Backup is unavailable')
    return folder


def _read(token):
    folder = _folder(token)
    marker = folder / MARKER
    if marker.is_symlink() or not marker.is_file():
        raise ValueError('Legacy or unregistered backup; not managed automatically')
    data = json.loads(marker.read_text('utf8'))
    if data.get('token') != token or not isinstance(data.get('expires_at'), int):
        raise ValueError('Invalid backup metadata')
    if not isinstance(data.get('installation_id'), str) or not isinstance(data.get('directory'), str):
        raise ValueError('Incomplete backup metadata')
    return folder, data


def register_backup(folder, installation_id, directory, previous_version, updated_version, previous_launcher):
    """Post-commit operation: never raise after catalog succeeds."""
    try:
        folder = Path(folder)
        if folder.parent != ROOT or not TOKEN.fullmatch(folder.name):
            raise ValueError('Unexpected backup path')
        data = dict(token=folder.name, installation_id=installation_id, directory=directory,
                    previous_version=previous_version, updated_version=updated_version, previous_launcher=previous_launcher,
                    created_at=int(time.time()), expires_at=int(time.time()) + LIFETIME)
        marker = folder / MARKER
        tmp = folder / (MARKER + '.tmp-' + uuid.uuid4().hex)
        try:
            with tmp.open('x', encoding='utf8') as handle:
                json.dump(data, handle)
            os.replace(tmp, marker)
        finally:
            tmp.unlink(missing_ok=True)
    except Exception:
        LOG.exception('Could not register update backup %s; retaining for manual review', folder)


def remove_unretained(folder):
    try:
        folder = Path(folder)
        if folder.parent != ROOT or folder.is_symlink() or not TOKEN.fullmatch(folder.name):
            raise ValueError('Unexpected backup path')
        shutil.rmtree(folder)
    except Exception:
        LOG.exception('Could not remove unretained update backup %s', folder)


def list_backups(installation_id):
    rows = []
    if not ROOT.is_dir() or ROOT.is_symlink():
        return rows
    for item in ROOT.iterdir():
        if not TOKEN.fullmatch(item.name):
            continue
        try:
            _, info = _read(item.name)
            if info['installation_id'] == installation_id:
                rows.append(info)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return sorted(rows, key=lambda x: x['created_at'], reverse=True)


def check_owner(token, game, installation_id):
    folder, info = _read(token)
    if info['directory'] != game or info['installation_id'] != installation_id:
        raise ValueError('Backup does not belong to this installation')
    return folder, info


def remove_backup(token, game, installation_id):
    folder, _ = check_owner(token, game, installation_id)
    shutil.rmtree(folder)


def expired_tokens():
    if not ROOT.is_dir() or ROOT.is_symlink():
        return []
    now = int(time.time())
    out = []
    for item in ROOT.iterdir():
        if not TOKEN.fullmatch(item.name):
            continue
        try:
            _, info = _read(item.name)
            if info['expires_at'] <= now:
                out.append(info)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return out

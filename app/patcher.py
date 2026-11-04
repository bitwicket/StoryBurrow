import hashlib
import json
import os
import re
import stat
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(os.getenv('STORYBURROW_PATCH_DIR', '/data/patches'))
MAX_FILES = 5000
MAX_UNCOMPRESSED = 1024 ** 3
LOCK = threading.RLock()
TOKEN = re.compile(r'^[a-f0-9]{32}$')

class PatchError(ValueError):
    pass

def _safe_path(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value or len(value) > 1024:
        raise PatchError('Invalid path in patch')
    path = PurePosixPath(value)
    parts = path.parts
    if value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/')) or not parts or ':' in parts[0]:
        raise PatchError('Absolute paths and parent traversal are not allowed')
    if any(ord(ch) < 32 for ch in value):
        raise PatchError('Control characters in file name')
    if parts[0] in ('.storyburrow-update-backups', '.storyburrow-uninstall-trash'):
        raise PatchError('Reserved folder in patch')
    if parts[:2] == ('game', 'saves') or parts[0] in ('saves',):
        raise PatchError('Patch cannot modify saved games')
    return str(path)

def _destination(game_dir, relative):
    relative = _safe_path(relative)
    if not game_dir.is_dir() or game_dir.is_symlink():
        raise PatchError('Game installation is missing or symlinked')
    current = game_dir
    parts = PurePosixPath(relative).parts
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise PatchError('Patch destination passes through a symbolic link')
        if current.exists() and not current.is_dir():
            raise PatchError('Patch destination has a non-directory parent')
    target = current / parts[-1]
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise PatchError('Patch destination is not a regular file')
    return target

def _hash_file(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def _write_json(path, data):
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    tmp.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(tmp, path)

def _stage_dir(token):
    if not TOKEN.fullmatch(token):
        raise PatchError('Invalid patch token')
    result = ROOT / 'staging' / token
    if not result.is_dir() or result.is_symlink():
        raise PatchError('Patch upload expired or missing')
    return result

def _read_stage(token, game_id, owner):
    folder = _stage_dir(token)
    state = json.loads((folder / 'state.json').read_text('utf-8'))
    if state['game_id'] != game_id or state['owner'] != owner:
        raise PatchError('Patch upload belongs to a different game or session')
    if time.time() - state['created'] > 24 * 3600:
        raise PatchError('Patch upload expired')
    return folder, state

def _zip_entries(archive):
    infos = [info for info in archive.infolist() if not info.is_dir()]
    if not infos or len(infos) > MAX_FILES:
        raise PatchError('ZIP must contain between 1 and 5000 files')
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise PatchError('ZIP expanded size exceeds 1 GiB')
    names = []
    for info in infos:
        name = _safe_path(info.filename)
        mode = (info.external_attr >> 16) & 0o170000
        if mode not in (0, 0o100000):
            raise PatchError('ZIP contains a link or non-regular file')
        if info.flag_bits & 1:
            raise PatchError('Encrypted ZIP archives are not supported')
        names.append(name)
    # Strip a single enclosing folder, but preserve actual game/ and lib/ roots.
    first = {PurePosixPath(n).parts[0] for n in names}
    strip = next(iter(first)) if len(first) == 1 and all('/' in n for n in names) and next(iter(first)).lower() not in ('game', 'lib', 'renpy', 'python') else None
    if len(set(names)) != len(names):
        raise PatchError('ZIP has duplicate entries')
    mapped = [n.split('/', 1)[1] if strip else n for n in names]
    mapped = [_safe_path(n) for n in mapped]
    if len(set(mapped)) != len(mapped) or len({n.casefold() for n in mapped}) != len(mapped):
        raise PatchError('ZIP has duplicate or case-conflicting destinations')
    return list(zip(infos, mapped)), strip

def create_stage(game_id, owner, uploaded, game_dir, single_destination=None):
    ROOT.joinpath('staging').mkdir(parents=True, exist_ok=True)
    if ROOT.is_symlink() or (ROOT / 'staging').is_symlink():
        raise PatchError('Patch storage cannot be symlinked')
    name = uploaded.filename or ''
    is_zip = name.lower().endswith('.zip')
    if not is_zip and not single_destination:
        single_destination = 'game/' + Path(name.replace('\\', '/')).name
    token = uuid.uuid4().hex
    stage = ROOT / 'staging' / token
    stage.mkdir(mode=0o700)
    try:
        payload = stage / 'payload'
        uploaded.save(payload)
        if not payload.is_file() or not payload.stat().st_size:
            raise PatchError('Uploaded file is empty')
        if is_zip:
            try:
                with zipfile.ZipFile(payload) as z:
                    entries, stripped = _zip_entries(z)
                    files = [{'source': info.filename, 'destination': dest, 'size': info.file_size} for info, dest in entries]
            except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
                raise PatchError('Invalid ZIP archive') from exc
        else:
            stripped = None
            files = [{'source': None, 'destination': _safe_path(single_destination), 'size': payload.stat().st_size}]
        for item in files:
            _destination(game_dir, item['destination'])
        state = {'game_id': game_id, 'owner': owner, 'created': time.time(),
                 'archive_name': name, 'kind': 'zip' if is_zip else 'file', 'stripped': stripped,
                 'files': files}
        _write_json(stage / 'state.json', state)
        return token, state
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise

def preview(game_dir, state):
    result = []
    for item in state['files']:
        target = _destination(game_dir, item['destination'])
        result.append({**item, 'action': 'overwrite' if target.exists() else 'add'})
    return result

def _atomic_copy(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Preserve executable bits on replacement files (Ren'Py launchers included).
    mode = stat.S_IMODE(dest.stat().st_mode) if dest.exists() else 0o644
    fd, temp = tempfile.mkstemp(prefix='.sb-patch-', dir=dest.parent)
    try:
        with os.fdopen(fd, 'wb') as out, Path(source).open('rb') as inp:
            shutil.copyfileobj(inp, out, length=1024 * 1024)
        os.chmod(temp, mode)
        os.replace(temp, dest)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def _extract_payload(stage, state, targets):
    extracted = stage / 'extracted'
    if extracted.exists():
        shutil.rmtree(extracted)
    extracted.mkdir()
    if state['kind'] == 'zip':
        with zipfile.ZipFile(stage / 'payload') as z:
            total = 0
            for index, item in enumerate(state['files']):
                # Match the inspected entry by index, never resolve ZIP paths.
                entry = z.infolist()
                info = next((i for i in entry if i.filename == item['source']), None)
                if info is None or info.file_size != item['size']:
                    raise PatchError('ZIP contents changed since preview')
                target = extracted / str(index)
                with z.open(info) as inp, target.open('wb') as out:
                    while True:
                        buf = inp.read(1024 * 1024)
                        if not buf:
                            break
                        total += len(buf)
                        if total > MAX_UNCOMPRESSED:
                            raise PatchError('ZIP extraction size exceeded')
                        out.write(buf)
    else:
        _atomic_copy(stage / 'payload', extracted / '0')
    return extracted

def apply(game_id, owner, token, game_dir):
    with LOCK:
        stage, state = _read_stage(token, game_id, owner)
        files = preview(game_dir, state)
        if not files:
            raise PatchError('No files to patch')
        extracted = _extract_payload(stage, state, files)
        history_root = ROOT / 'history' / game_id
        history_root.mkdir(parents=True, exist_ok=True)
        if (ROOT / 'history').is_symlink() or history_root.is_symlink():
            raise PatchError('Patch history cannot be symlinked')
        patch_id = uuid.uuid4().hex
        entry = history_root / patch_id
        entry.mkdir(mode=0o700)
        (entry / 'originals').mkdir()
        records = []
        changed = []
        try:
            # All originals are copied before making any change.
            for idx, item in enumerate(files):
                target = _destination(game_dir, item['destination'])
                existed = target.exists()
                record = {'destination': item['destination'], 'existed': existed,
                          'before_sha256': _hash_file(target) if existed else None,
                          'after_sha256': _hash_file(extracted / str(idx))}
                if existed:
                    _atomic_copy(target, entry / 'originals' / str(idx))
                records.append(record)
            # Detect concurrent external modifications, including during the copy.
            for record in records:
                target = _destination(game_dir, record['destination'])
                if (target.exists() != record['existed'] or
                    (record['existed'] and _hash_file(target) != record['before_sha256'])):
                    raise PatchError('Game files changed during patch preparation')
            for idx, record in enumerate(records):
                target = _destination(game_dir, record['destination'])
                _atomic_copy(extracted / str(idx), target)
                changed.append(idx)
            manifest = {'id': patch_id, 'game_id': game_id, 'name': state['archive_name'],
                        'installed_at': time.time(), 'files': records, 'status': 'installed'}
            _write_json(entry / 'manifest.json', manifest)
        except Exception as exc:
            errors = []
            for idx in reversed(changed):
                record = records[idx]
                try:
                    target = _destination(game_dir, record['destination'])
                    if record['existed']:
                        _atomic_copy(entry / 'originals' / str(idx), target)
                    else:
                        target.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    errors.append(str(rollback_exc))
            if not errors:
                shutil.rmtree(entry, ignore_errors=True)
            if errors:
                raise PatchError('Patch failed; automatic recovery incomplete: ' + '; '.join(errors)) from exc
            raise
        finally:
            shutil.rmtree(extracted, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)
        return manifest

def history(game_id):
    folder = ROOT / 'history' / game_id
    if not folder.exists():
        return []
    if folder.is_symlink():
        raise PatchError('Patch history is symlinked')
    result = []
    for item in folder.iterdir():
        if item.is_dir() and not item.is_symlink() and TOKEN.fullmatch(item.name) and (item / 'manifest.json').is_file():
            result.append(json.loads((item / 'manifest.json').read_text('utf-8')))
    return sorted(result, key=lambda row: row['installed_at'], reverse=True)

def revert(game_id, patch_id, game_dir):
    with LOCK:
        if not TOKEN.fullmatch(patch_id):
            raise PatchError('Invalid patch ID')
        entry = ROOT / 'history' / game_id / patch_id
        if not entry.is_dir() or entry.is_symlink():
            raise PatchError('Patch history entry not found')
        manifest = json.loads((entry / 'manifest.json').read_text('utf-8'))
        if manifest['status'] != 'installed' or manifest['game_id'] != game_id:
            raise PatchError('Patch is not active')
        # Reject intervening patches that touch any of these files, even if hashes match.
        destinations = {f['destination'] for f in manifest['files']}
        for later in history(game_id):
            if later['status'] == 'installed' and later['installed_at'] > manifest['installed_at']:
                if destinations.intersection(f['destination'] for f in later['files']):
                    raise PatchError('Revert newer overlapping patches first')
        for idx, record in enumerate(manifest['files']):
            target = _destination(game_dir, record['destination'])
            if not target.is_file() or _hash_file(target) != record['after_sha256']:
                raise PatchError('Patched file was changed or removed: ' + record['destination'])
            if record['existed']:
                backup = entry / 'originals' / str(idx)
                if not backup.is_file() or _hash_file(backup) != record['before_sha256']:
                    raise PatchError('Patch backup is missing or corrupt')
        # Stage currently patched contents so a failed revert can be rolled back.
        emergency = entry / ('recovery-' + uuid.uuid4().hex)
        emergency.mkdir()
        changed = []
        try:
            for idx, record in enumerate(manifest['files']):
                _atomic_copy(_destination(game_dir, record['destination']), emergency / str(idx))
            for idx, record in enumerate(manifest['files']):
                target = _destination(game_dir, record['destination'])
                if record['existed']:
                    _atomic_copy(entry / 'originals' / str(idx), target)
                else:
                    target.unlink()
                changed.append(idx)
            manifest['status'] = 'reverted'
            manifest['reverted_at'] = time.time()
            _write_json(entry / 'manifest.json', manifest)
        except Exception as exc:
            errors = []
            for idx in reversed(changed):
                try:
                    _atomic_copy(emergency / str(idx), _destination(game_dir, manifest['files'][idx]['destination']))
                except Exception as rollback_exc:
                    errors.append(str(rollback_exc))
            if errors:
                raise PatchError('Reversion failed; recovery incomplete: ' + '; '.join(errors)) from exc
            raise
        finally:
            shutil.rmtree(emergency, ignore_errors=True)
        return manifest



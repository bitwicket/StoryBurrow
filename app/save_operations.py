import json
import os
import re
import shutil
import threading
import time
import uuid
from save_time import local_time
from pathlib import Path

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._ -]{0,199}$')
TTL = 24 * 3600
LOCK = threading.RLock()

def safe_file(name):
    if not isinstance(name, str) or name in ('.', '..') or not NAME.fullmatch(name) or not (name.endswith('.save') or name == 'persistent'):
        raise ValueError('Only simple .save filenames and persistent are supported')
    return name

def directory(path, parent):
    return path.is_dir() and not path.is_symlink() and path.resolve().parent == parent.resolve()

def _file(path):
    return path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1

def _recover_root(root):
    rec = root / 'recovery'
    if rec.is_symlink() or (rec.exists() and not directory(rec, root)):
        raise ValueError('Unsafe recovery directory')
    rec.mkdir(exist_ok=True, mode=0o700)
    return rec

def cleanup(root):
    rec = _recover_root(root)
    now = time.time()
    for child in rec.iterdir():
        if not directory(child, rec):
            continue
        meta = child / 'recovery.json'
        try:
            data = json.loads(meta.read_text()) if _file(meta) else {}
            if data.get('state') == 'complete' and isinstance(data.get('expires'), (int, float)) and data['expires'] < now:
                shutil.rmtree(child)
        except (ValueError, OSError, TypeError):
            continue

def backup(root, paths, action):
    rec = _recover_root(root)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    folder = rec / f'{stamp}-{action}-{uuid.uuid4().hex[:10]}'
    folder.mkdir(mode=0o700)
    meta = {'state': 'pending', 'action': action, 'created': int(time.time()),
            'expires': int(time.time()) + TTL, 'files': [p.name for p in paths]}
    (folder / 'recovery.json').write_text(json.dumps(meta, indent=2))
    try:
        for p in paths:
            if not _file(p):
                raise ValueError(f'Unsafe or missing save: {p.name}')
            shutil.copy2(p, folder / p.name, follow_symlinks=False)
    except Exception:
        # Retain uncertain backup for manual inspection.
        raise
    return folder, meta

def finish(folder, meta):
    meta['state'] = 'complete'
    temp = folder / ('recovery.json.tmp-' + uuid.uuid4().hex)
    temp.write_text(json.dumps(meta, indent=2))
    os.replace(temp, folder / 'recovery.json')

def replace(root, name, source, action='overwrite'):
    name = safe_file(name)
    current = root / 'current'
    if not directory(current, root) or not _file(source):
        raise ValueError('Unsafe save source or destination')
    target = current / name
    if target.is_symlink() or (target.exists() and not _file(target)):
        raise ValueError('Destination is unsafe')
    recovery = None
    if target.exists():
        recovery = backup(root, [target], action)
    tmp = current / ('.storyburrow-write-' + uuid.uuid4().hex)
    try:
        shutil.copyfile(source, tmp, follow_symlinks=False)
        os.replace(tmp, target)
        if recovery:
            finish(*recovery)
    finally:
        tmp.unlink(missing_ok=True)
    return bool(recovery)

def delete(root, name):
    target = root / 'current' / safe_file(name)
    if not _file(target):
        raise ValueError('Save not found or unsafe')
    rec = backup(root, [target], 'delete')
    target.unlink()
    finish(*rec)

def restore_snapshot(root, snapshot, name=None):
    if not isinstance(snapshot, str) or snapshot in ('.', '..') or not NAME.fullmatch(snapshot):
        raise ValueError('Invalid snapshot identifier')
    snaps = root / 'snapshots'
    folder = snaps / snapshot
    if not directory(snaps, root) or not directory(folder, snaps):
        raise ValueError('Snapshot is missing or unsafe')
    files = ([folder / safe_file(name)] if name is not None else
             [p for p in folder.iterdir() if p.is_file() and not p.is_symlink() and (p.name == 'persistent' or p.suffix == '.save')])
    if not files:
        raise ValueError('Snapshot contains no supported saves')
    # For full restore, preserve current nonmatching saves, avoiding unintended deletions.
    for source in files:
        if not _file(source):
            raise ValueError('Snapshot file is unsafe')
    for source in files:
        replace(root, source.name, source, 'snapshot-restore')
    return len(files)

def restore_recovery(root, token):
    if not re.fullmatch(r'\d{8}T\d{6}Z-[a-z-]+-[a-f0-9]{10}', token or ''):
        raise ValueError('Invalid recovery entry')
    rec = _recover_root(root)
    folder = rec / token
    if not directory(folder, rec):
        raise ValueError('Recovery entry unavailable')
    meta_file = folder / 'recovery.json'
    if not _file(meta_file):
        raise ValueError('Recovery metadata unavailable')
    meta = json.loads(meta_file.read_text())
    if meta.get('state') != 'complete' or meta.get('expires', 0) < time.time():
        raise ValueError('Recovery entry expired or incomplete')
    files = [folder / safe_file(f) for f in meta.get('files', [])]
    if not files or any(not _file(p) for p in files):
        raise ValueError('Recovery files unavailable')
    for p in files:
        replace(root, p.name, p, 'recovery-restore')
    return len(files)

def recoveries(root):
    result = []
    rec = _recover_root(root)
    for p in sorted(rec.iterdir(), reverse=True):
        if not directory(p, rec):
            continue
        try:
            m = json.loads((p/'recovery.json').read_text())
            if m.get('state') == 'complete' and m.get('expires', 0) > time.time():
                result.append({'token':p.name, 'action':m.get('action'), 'files':m.get('files',[]), 'expires':local_time(m['expires'])})
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return result[:100]

def start_cleanup_worker(manager_command, interval=3600):
    def sweep():
        from catalog import connect
        from save_routing import SAVES
        from save_manager_routes import SAFE_NAME
        while True:
            try:
                status = manager_command('status')
                if status.get('ok') and not status.get('running'):
                    with connect() as db:
                        rows = db.execute("SELECT managed_save_namespace FROM installations WHERE status='installed' AND save_mode='managed'").fetchall()
                    with LOCK:
                        for row in rows:
                            ns = row['managed_save_namespace']
                            if ns and SAFE_NAME.fullmatch(ns):
                                root = SAVES / ns
                                if directory(root, SAVES):
                                    cleanup(root)
            except Exception:
                import logging
                logging.getLogger('storyburrow.saves').exception('Recovery cleanup sweep failed')
            time.sleep(interval)
    threading.Thread(target=sweep, name='storyburrow-recovery-cleanup', daemon=True).start()

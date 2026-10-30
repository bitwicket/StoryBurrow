"""Import exported StoryBurrow snapshots without restoring active saves."""
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import uuid
import zipfile

from flask import Blueprint, abort, flash, redirect, request, session
from werkzeug.exceptions import HTTPException
from flask import render_template
from save_actions_routes import guarded_root

bp = Blueprint('snapshot_import', __name__)
MAX_COMPRESSED = 256 * 1024 * 1024
MAX_UNCOMPRESSED = 512 * 1024 * 1024
MAX_FILES = 10000
MAX_MEMBER = 128 * 1024 * 1024
LABEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')

@bp.errorhandler(HTTPException)
def error_page(err):
    return render_template('message.html', title='Snapshot import unavailable', message=err.description), err.code

def _safe_name(filename):
    if not isinstance(filename, str) or '\\' in filename or '\x00' in filename:
        raise ValueError('Unsupported ZIP path')
    parts = PurePosixPath(filename).parts
    if (not parts or filename.startswith('/') or any(p in ('', '.', '..') for p in parts)
            or ':' in parts[0] or len(parts) > 12 or len(filename) > 500):
        raise ValueError('Unsafe ZIP path')
    return parts

def _validate_manifest(stage):
    # Both native update snapshots and pre-adoption snapshots are supported.
    for label in ('.storyburrow-manifest.json', 'manifest.json'):
        file = stage / label
        if not file.is_file():
            continue
        try:
            manifest = json.loads(file.read_text('utf-8'))
        except (OSError, ValueError) as exc:
            raise ValueError('Snapshot manifest is unreadable') from exc
        records = manifest.get('files')
        if isinstance(records, list):
            entries = [(r['path'], r['sha256']) for r in records]
        elif isinstance(records, dict):
            entries = list(records.items())
        else:
            raise ValueError('Snapshot manifest has unsupported format')
        for name, expected in entries:
            _safe_name(name)
            if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', expected):
                raise ValueError('Invalid manifest digest')
            candidate = stage.joinpath(*PurePosixPath(name).parts)
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError('Manifest references missing snapshot file')
            with candidate.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if not hmac.compare_digest(digest, expected.lower()):
                raise ValueError('Snapshot checksum mismatch: ' + name)
        return manifest
    raise ValueError('A StoryBurrow snapshot manifest is required')


def extract_verified(archive_path, destination):
    """Bounded extraction to a new empty directory; throws on bad/malformed ZIPs."""
    total = 0
    seen = set()
    saves = 0
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_FILES:
            raise ValueError('Unsupported number of ZIP entries')
        for item in infos:
            parts = _safe_name(item.filename.rstrip('/'))
            if item.filename.rstrip('/') in seen:
                raise ValueError('Duplicate ZIP path')
            seen.add(item.filename.rstrip('/'))
            mode = (item.external_attr >> 16) & 0xFFFF
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError('ZIP contains a symlink or unsupported entry')
            if item.is_dir():
                continue
            if item.file_size > MAX_MEMBER:
                raise ValueError('Oversized ZIP entry')
            total += item.file_size
            if total > MAX_UNCOMPRESSED:
                raise ValueError('Snapshot exceeds uncompressed size limit')
            if parts[-1].endswith('.save') or parts[-1] == 'persistent':
                saves += 1
            dest = destination.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, dest.open('xb') as target:
                copied = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk: break
                    copied += len(chunk)
                    if copied > item.file_size: raise ValueError('ZIP size mismatch')
                    target.write(chunk)
                if copied != item.file_size:
                    raise ValueError('Truncated snapshot entry')
    if not saves:
        raise ValueError('Archive has no save data')
    return _validate_manifest(destination)

@bp.post('/games/<game_id>/storage/snapshots/import')
def import_snapshot(game_id):
    if not hmac.compare_digest(request.form.get('csrf_token', ''), session.get('_csrf', 'not-set')):
        abort(400, description='Invalid form token')
    if request.form.get('acknowledge') != 'yes':
        abort(400, description='Confirm the snapshot is from a source you trust')
    root = guarded_root(game_id)  # Catalog/managed routing and universal game-running lock.
    base = root / 'snapshots'
    if base.is_symlink() or (base.exists() and not base.is_dir()):
        abort(409, description='Unsafe snapshot directory')
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    upload = request.files.get('snapshot')
    if upload is None or not (upload.filename or '').lower().endswith('.zip'):
        abort(400, description='Select a StoryBurrow snapshot ZIP')
    stage = base / ('.importing-' + uuid.uuid4().hex)
    upload_path = base / ('.upload-' + uuid.uuid4().hex + '.zip')
    stage.mkdir(mode=0o700)
    try:
        size = 0
        with upload_path.open('xb') as target:
            while True:
                chunk = upload.stream.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > MAX_COMPRESSED: raise ValueError('Snapshot ZIP exceeds upload limit')
                target.write(chunk)
        if not zipfile.is_zipfile(upload_path):
            raise ValueError('Invalid ZIP file')
        manifest = extract_verified(upload_path, stage)
        # Never reuse imported labels, even for the same source snapshot.
        label = 'imported_' + uuid.uuid4().hex[:12]
        if not LABEL.fullmatch(label): raise ValueError('Invalid snapshot name')
        stage.rename(base / label)
        source = manifest.get('installation') or manifest.get('game') or 'unknown'
        flash(f'Snapshot imported as {label}. Source: {source}. Current saves were not changed.', 'success')
        return redirect(f'/games/{game_id}/saves?tab=snapshots', code=303)
    except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile, KeyError, TypeError) as exc:
        abort(409, description=f'Snapshot import failed: {exc}')
    finally:
        upload_path.unlink(missing_ok=True)
        if stage.exists(): shutil.rmtree(stage)

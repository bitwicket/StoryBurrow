from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

def _valid(text: str, what: str) -> str:
    if not COMPONENT.fullmatch(text) or text in {'.', '..'}:
        raise ValueError(f"Invalid {what}: {text!r}")
    return text

def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def _inventory(folder: Path):
    result = []
    for root, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            path = Path(root) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise ValueError(f"Non-regular save entry refused: {path}")
            if stat.S_ISREG(mode):
                result.append((path.relative_to(folder).as_posix(), path.stat().st_size, _hash(path)))
    return sorted(result)

def snapshot(root: Path, installation: str, version: str) -> Path:
    _valid(installation, 'installation directory')
    _valid(version, 'version')
    root = root.resolve(strict=True)
    base = root / installation
    current = base / 'current'
    if base.is_symlink() or current.is_symlink() or not current.is_dir():
        raise ValueError('Expected existing managed installation/current directory')
    # Only files inside a managed namespace are copied; never copy game directories.
    before = _inventory(current)
    if not before:
        raise ValueError('No save files in current/; refusing empty snapshot')
    snapshots = base / 'snapshots'
    if snapshots.is_symlink():
        raise ValueError('Snapshots directory must not be a symlink')
    snapshots.mkdir(mode=0o700, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    label = f'{version}_{timestamp}_{uuid.uuid4().hex[:8]}'
    dest = snapshots / label
    temp = snapshots / ('.building-' + uuid.uuid4().hex)
    try:
        shutil.copytree(current, temp, symlinks=True)
        copied = _inventory(temp)
        if copied != before or _inventory(current) != before:
            raise RuntimeError('Save files changed while snapshotting or copy verification failed')
        # Manifest outside the snapshot payload so copied content compares exactly.
        (temp / '.storyburrow-manifest.json').write_text(json.dumps({
            'installation': installation, 'source_version': version,
            'created_utc': timestamp,
            'files': [{'path':p, 'bytes':n, 'sha256':s} for p,n,s in copied],
        }, indent=2) + '\n')
        temp.rename(dest)
        return dest
    except BaseException:
        if temp.exists():
            shutil.rmtree(temp)
        raise

def main():
    parser = argparse.ArgumentParser(description='Create a verified StoryBurrow managed save snapshot')
    parser.add_argument('--root', default='/saves')
    parser.add_argument('--installation', required=True, help='UUID-scoped directory basename, e.g. OriginStory-abc123')
    parser.add_argument('--from-version', required=True)
    args = parser.parse_args()
    print(snapshot(Path(args.root), args.installation, args.from_version))

if __name__ == '__main__':
    main()

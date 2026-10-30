from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAX_FILES = 100_000
MAX_TOTAL_UNPACKED = 40 * 1024**3
MAX_MEMBER_BYTES = 12 * 1024**3
MAX_OPTIONS_BYTES = 512_000
VALID_LAUNCHER = re.compile(r"^[^/\\]+\.sh$", re.I)
TITLE_RE = re.compile(r'^\s*define\s+config\.(?:name|window_title)\s*=\s*([\"\'])(.*?)\1', re.M)
VERSION_RE = re.compile(r'^\s*define\s+config\.version\s*=\s*([\"\'])(.*?)\1', re.M)
BAD_PARTS = {'.', '..', ''}


class InspectionError(ValueError):
    pass


def normalize_path(raw):
    if not isinstance(raw, str) or not raw or '\\' in raw or '\x00' in raw:
        raise InspectionError('Invalid archive path')
    if raw.startswith('/') or re.match(r'^[A-Za-z]:', raw):
        raise InspectionError('Absolute archive path is not permitted')
    value = raw.rstrip('/')
    parts = value.split('/')
    if any(part in BAD_PARTS for part in parts):
        raise InspectionError('Unsafe path component')
    if len(value) > 1024 or len(parts) > 32:
        raise InspectionError('Path too deep or long')
    return '/'.join(parts)


def source_contents(source: Path):
    if source.is_file():
        if not zipfile.is_zipfile(source):
            raise InspectionError('Only ZIP archives or extracted folders are supported')
        zf = zipfile.ZipFile(source)
        entries = zf.infolist()
        if len(entries) > MAX_FILES:
            zf.close()
            raise InspectionError('Too many archive entries')
        names = set()
        original_by_name = {}
        total = 0
        for entry in entries:
            name = normalize_path(entry.filename)
            if name in names:
                zf.close()
                raise InspectionError('Duplicate archive path')
            names.add(name)
            original_by_name[name] = entry
            mode = (entry.external_attr >> 16)
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                zf.close()
                raise InspectionError('Archive contains links or special files')
            if entry.flag_bits & 0x1:
                zf.close()
                raise InspectionError('Encrypted ZIP entries are unsupported')
            if entry.file_size > MAX_MEMBER_BYTES:
                zf.close()
                raise InspectionError('Archive member exceeds size limit')
            total += entry.file_size
            if total > MAX_TOTAL_UNPACKED:
                zf.close()
                raise InspectionError('Archive exceeds total uncompressed size limit')
        def reader(name):
            entry = original_by_name[name]
            if entry.file_size > MAX_OPTIONS_BYTES:
                return None
            with zf.open(entry) as f:
                return f.read(MAX_OPTIONS_BYTES + 1)[:MAX_OPTIONS_BYTES]
        return 'zip', names, reader, source.stat().st_size, zf.close

    if source.is_dir():
        names = set()
        files = {}
        size = 0
        for path in source.rglob('*'):
            if path.is_symlink():
                raise InspectionError('Folder contains symbolic links')
            relative = normalize_path(path.relative_to(source).as_posix())
            names.add(relative)
            if len(names) > MAX_FILES:
                raise InspectionError('Too many files')
            if path.is_file():
                files[relative] = path
                length = path.stat().st_size
                if length > MAX_MEMBER_BYTES:
                    raise InspectionError('File exceeds size limit')
                size += length
                if size > MAX_TOTAL_UNPACKED:
                    raise InspectionError('Folder exceeds size limit')
        def reader(name):
            path = files.get(name)
            if not path or path.stat().st_size > MAX_OPTIONS_BYTES:
                return None
            return path.read_bytes()
        return 'folder', names, reader, size, lambda: None
    raise InspectionError('Source does not exist')


def game_roots(names):
    roots = set()
    for name in names:
        parts = name.split('/')
        for i, part in enumerate(parts):
            if part == 'game' and i + 1 < len(parts):
                roots.add('/'.join(parts[:i]))
    results = []
    for root in sorted(roots):
        prefix = (root + '/') if root else ''
        launchers = sorted(name for name in names
                           if name.startswith(prefix) and '/' not in name[len(prefix):]
                           and VALID_LAUNCHER.fullmatch(name[len(prefix):]))
        # Must contain actual game files, not just a 'game' folder.
        if launchers:
            results.append((root, launchers))
    # Nested roots often represent dependencies, not top-level games.
    return results


def clean_name(value):
    value = re.sub(r'(?i)(?:[-_ ]+(?:pc|linux|win|mac|windows))$', '', value)
    version_match = re.search(r'(?i)(?:[-_ ]+v?)(\d+(?:\.\d+){1,4}(?:[-a-z0-9.]*)?)$', value)
    version = version_match.group(1) if version_match else None
    if version_match:
        value = value[:version_match.start()]
    title = re.sub(r'[_-]+', ' ', value).strip()
    return title or 'Unknown Game', version


def parse_options(blob):
    if not blob:
        return {}
    text = blob.decode('utf-8-sig', errors='replace')
    title = TITLE_RE.search(text)
    version = VERSION_RE.search(text)
    # Static string literals only: no evaluation of Ren'Py code.
    return {'title': title.group(2) if title else None,
            'version': version.group(2) if version else None}


def inspect(source):
    source = Path(source).expanduser().resolve()
    kind, names, reader, source_bytes, close = source_contents(source)
    try:
        roots = game_roots(names)
        if not roots:
            raise InspectionError('No Ren\'Py game root with a Linux .sh launcher found')
        if len(roots) != 1:
            raise InspectionError(f'Ambiguous ZIP/folder: found {len(roots)} possible game roots')
        root, launchers = roots[0]
        if len(launchers) != 1:
            raise InspectionError('Multiple Linux launchers: manual selection needed')
        options_path = (root + '/' if root else '') + 'game/options.rpy'
        options = parse_options(reader(options_path)) if options_path in names else {}
        folder_title, folder_ver = clean_name(root.split('/')[-1] if root else (source.stem if kind == 'zip' else source.name))
        archive_title, archive_ver = clean_name(source.stem)
        title = options.get('title') or folder_title or archive_title
        version = options.get('version') or folder_ver or archive_ver
        # Stable UUID is assigned at review/installation; avoid title-derived IDs.
        return {
            'schema_version': 1,
            'status': 'inspection_only',
            'detected': {
                'title': {'value': title, 'source': 'game/options.rpy' if options.get('title') else ('root_folder' if root else 'source_name'), 'editable': True},
                'installed_version': {'value': version, 'source': 'game/options.rpy' if options.get('version') else ('root_folder' if folder_ver else 'source_name' if archive_ver else 'unknown'), 'editable': True},
                'launcher': launchers[0],
                'root_prefix': root,
            },
            'identity': {'game_id': None, 'vndb_id': None, 'metadata_reviewed': False},
            'source': {'type': 'zip_upload' if kind == 'zip' else 'folder_upload',
                       'name': source.name, 'bytes': source_bytes, 'url': None, 'platform': None},
            'external_metadata': {'provider': None, 'matched_id': None, 'confirmed': False, 'retrieved_at': None},
            'artwork': {'local_cover': None, 'selected_cover': None, 'source': None},
            'timestamps': {'inspected_at': datetime.now(timezone.utc).isoformat(), 'installed_at': None},
            'technical': {'file_count': len(names), 'has_renpy_runtime': any(n.startswith((root + '/' if root else '') + 'renpy/') for n in names),
                          'has_lib_directory': any(n.startswith((root + '/' if root else '') + 'lib/') for n in names)},
        }
    finally:
        close()

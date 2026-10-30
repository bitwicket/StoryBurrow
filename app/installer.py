import json
import os
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path

from importer.inspector import InspectionError, inspect, normalize_path, MAX_TOTAL_UNPACKED, MAX_FILES

GAMES = Path(os.environ.get('STORYBURROW_GAMES_DIR', '/games')).resolve()
TEMP = GAMES / '.storyburrow-installing'
VALID_DIR = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$')
BUFFER = 1024 * 1024


class InstallError(Exception):
    pass


def target_name(title):
    name = re.sub(r'[^A-Za-z0-9._ -]+', '', title).strip(' .-_')
    name = re.sub(r'\s+', '-', name)[:100]
    if not name or not VALID_DIR.fullmatch(name) or name in ('.', '..'):
        raise InstallError('Invalid installation directory name')
    return name


def safe_members(zf, prefix):
    seen = set()
    files = []
    total = 0
    for info in zf.infolist():
        name = normalize_path(info.filename)
        if name in seen:
            raise InstallError('Duplicate path in ZIP')
        seen.add(name)
        if len(seen) > MAX_FILES:
            raise InstallError('Too many archive members')
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise InstallError('ZIP contains a symbolic link or special file')
        if info.flag_bits & 1:
            raise InstallError('Encrypted ZIP unsupported')
        if info.file_size > 12 * 1024**3:
            raise InstallError('File exceeds extraction size limit')
        total += info.file_size
        if total > MAX_TOTAL_UNPACKED:
            raise InstallError('Archive exceeds extraction size limit')
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if not rel:
            continue
        if not info.is_dir():
            files.append((info, rel))
    return files


def safe_destination(root, rel):
    rel = normalize_path(rel)
    output = root.joinpath(*rel.split('/'))
    if not output.is_relative_to(root):
        raise InstallError('Unsafe extraction destination')
    return output


def copy_bounded(source, dest, budget):
    with source as src, dest.open('xb') as out:
        while True:
            block = src.read(BUFFER)
            if not block:
                break
            budget[0] += len(block)
            if budget[0] > MAX_TOTAL_UNPACKED:
                raise InstallError('Extraction exceeds allowed size')
            out.write(block)


def prepare_windowless_permissions(directory, launcher):
    for path in directory.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        executable = path.suffix.lower() == '.sh'
        if not executable:
            with path.open('rb') as f:
                executable = f.read(4) == b'\x7fELF'
        if executable:
            path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    launcher_path = safe_destination(directory, launcher)
    if not launcher_path.is_file():
        raise InstallError('Launcher missing after extraction')
    launcher_path.chmod(launcher_path.stat().st_mode | stat.S_IXUSR)


def prepare(source, folder_name, destination_root=GAMES):
    dest_root = Path(destination_root).resolve()
    stage_parent = dest_root / '.storyburrow-installing'
    stage_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not VALID_DIR.fullmatch(folder_name) or folder_name in ('.', '..'):
        raise InstallError('Invalid installation directory')
    if (dest_root / folder_name).exists():
        raise InstallError('Installation directory already exists')
    source = Path(source).resolve()
    inspected = inspect(source)
    prefix = inspected['detected']['root_prefix']
    launcher = inspected['detected']['launcher']
    launch_rel = launcher[len(prefix) + 1:] if prefix else launcher
    if '/' in launch_rel or not launch_rel.endswith('.sh'):
        raise InstallError('Unsupported launcher location')
    pending = stage_parent / ('pending-' + uuid.uuid4().hex)
    pending.mkdir(mode=0o700)
    try:
        budget = [0]
        if source.is_file():
            with zipfile.ZipFile(source) as zf:
                members = safe_members(zf, prefix + '/' if prefix else '')
                for info, rel in members:
                    dest = safe_destination(pending, rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    copy_bounded(zf.open(info), dest, budget)  # ZIP CRC checked on full read
        else:
            original = source / prefix if prefix else source
            for item in original.rglob('*'):
                if item.is_symlink():
                    raise InstallError('Folder contains symbolic links')
                if item.is_dir():
                    continue
                if not item.is_file():
                    raise InstallError('Folder contains unsupported entry')
                rel = item.relative_to(original).as_posix()
                dest = safe_destination(pending, rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                copy_bounded(item.open('rb'), dest, budget)
        prepare_windowless_permissions(pending, launch_rel)
        # Final validation against StoryBurrow's actual launch discovery convention.
        if not (pending / 'game').is_dir():
            raise InstallError('Game directory missing after extraction')
        launchers = list(pending.glob('*.sh'))
        if len(launchers) != 1 or launchers[0].name != launch_rel:
            raise InstallError('Multiple or missing Linux launchers')
        return pending, launch_rel, inspected
    except Exception:
        shutil.rmtree(pending, ignore_errors=True)
        raise

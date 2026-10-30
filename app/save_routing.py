#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import uuid
from pathlib import Path

GAMES = Path(os.environ.get('STORYBURROW_GAMES_DIR', '/games')).resolve()
SAVES = Path(os.environ.get('STORYBURROW_SAVES_DIR', '/saves')).resolve()
CONFIG = Path(os.environ.get('STORYBURROW_ROUTING_CONFIG', '/data/managed-save-routing.json'))
VALID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$')


def _valid(value):
    if not isinstance(value, str) or value in ('.','..') or not VALID.fullmatch(value):
        raise ValueError('Invalid name')
    return value


def _paths(game, namespace):
    _valid(game)
    _valid(namespace)
    directory = GAMES / game
    if directory.is_symlink() or not directory.is_dir() or not (directory / 'game').is_dir():
        raise ValueError('Game directory missing or symbolic link')
    if directory.resolve().parent != GAMES:
        raise ValueError('Game escapes games root')
    target = SAVES / namespace
    if target.is_symlink() or target.resolve().parent != SAVES:
        raise ValueError('Managed save directory escapes saves root')
    return directory, target


def _read_config():
    if not CONFIG.is_file():
        return {}
    obj = json.loads(CONFIG.read_text())
    if not isinstance(obj, dict):
        raise ValueError('Routing configuration must be an object')
    return obj


def _write_config(config):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_name(CONFIG.name + '.tmp-' + uuid.uuid4().hex)
    try:
        tmp.write_text(json.dumps(config, indent=2, sort_keys=True) + '\n')
        os.replace(tmp, CONFIG)
    finally:
        tmp.unlink(missing_ok=True)


def _copy_contents(src, dst):
    if not src.is_dir() or src.is_symlink():
        raise ValueError('Local saves must be a real directory')
    for child in src.iterdir():
        if child.is_symlink():
            raise ValueError('Refusing symbolic links inside original saves')
        destination = dst / child.name
        if destination.exists() or destination.is_symlink():
            raise ValueError('Cannot merge into existing managed saves: ' + str(destination))
        if child.is_file():
            shutil.copy2(child, destination)
        elif child.is_dir():
            shutil.copytree(child, destination, symlinks=False)
        else:
            raise ValueError('Unsupported save entry: ' + str(child))


def prepare(game, namespace, save_id=None):
    directory, root = _paths(game, namespace)
    if save_id is not None:
        _valid(save_id)
    config = _read_config()
    if game in config:
        raise ValueError('Game already routed; use inspect or disable first')
    if any(item.get('namespace') == namespace for item in config.values()):
        raise ValueError('Namespace is already assigned to another game')
    local = directory / 'game' / 'saves'
    if local.is_symlink():
        raise ValueError('Game-local saves already a symlink; refusing to modify')
    if local.exists() and not local.is_dir():
        raise ValueError('Unexpected game-local saves path type')
    if root.exists():
        raise ValueError('Namespace already exists (refusing to overwrite)')
    root.mkdir(parents=True)
    current = root / 'current'
    current.mkdir()
    parent = root / 'renpy-parent'
    parent.mkdir()
    backup = root / 'legacy-local-original'
    moved = False
    linked = False
    original_local = directory / 'game' / 'saves.storyburrow-original'
    if original_local.exists() or original_local.is_symlink():
        raise ValueError('Original local backup path already exists: ' + str(original_local))
    try:
        if local.exists():
            backup.mkdir()
            _copy_contents(local, backup)
            _copy_contents(local, current)
            local.rename(original_local)
            moved = True
        local.symlink_to(current, target_is_directory=True)
        linked = True
        if save_id:
            # Isolate Ren'Py user path even when two games have same native ID.
            (parent / save_id).symlink_to(current, target_is_directory=True)
        config[game] = {'namespace': namespace, 'save_id': save_id, 'parent': str(parent)}
        _write_config(config)
    except Exception:
        if linked and local.is_symlink():
            local.unlink()
        if moved:
            original_local.rename(local)
        raise
    return {'game': game, 'current': str(current), 'parent': str(parent),
            'legacy_backup': str(backup) if moved else None,
            'save_id': save_id, 'note': 'Do not uninstall until the routing lifecycle is integrated'}


def disable(game):
    _valid(game)
    config = _read_config()
    entry = config.get(game)
    if entry is None:
        raise ValueError('No managed routing configured for game')
    directory, root = _paths(game, entry['namespace'])
    local = directory / 'game' / 'saves'
    current = root / 'current'
    if not local.is_symlink() or local.resolve() != current.resolve():
        raise ValueError('Game-local saves were changed externally; manual recovery needed')
    recovered = directory / 'game' / ('saves.storyburrow-restoring-' + uuid.uuid4().hex[:8])
    recovered.mkdir()
    try:
        _copy_contents(current, recovered)
    except Exception:
        raise
    local.unlink()
    recovered.rename(local)
    config.pop(game)
    _write_config(config)
    return {'restored_game_local': str(local), 'managed_copy_retained': str(current)}


def inspect():
    config = _read_config()

    for game, entry in sorted(config.items()):
        namespace = entry.get('namespace', '')
        save_id = entry.get('save_id')
        parent = entry.get('parent')

        print(f'{game}: namespace={namespace} save_id={save_id}')
        print(f'  parent={parent}')

        try:
            _valid(game)
            _valid(namespace)

            directory = GAMES / game
            root = SAVES / namespace
            current = root / 'current'
            local = directory / 'game' / 'saves'

            if directory.is_symlink() or not directory.is_dir():
                print('  status=ORPHANED (game directory missing)')
                print(f'  current={current}')
                print(f'  saves_preserved={root.is_dir()}')
                continue

            if root.is_symlink():
                print('  status=WARNING (save namespace is a symlink)')
                continue

            print(f'  current={current}')
            print(f'  current_exists={current.is_dir()}')

            if local.is_symlink():
                print(f'  local_link=True target={local.resolve()}')
                print(f'  routing_valid={local.resolve() == current.resolve()}')
            else:
                print('  local_link=False')
                print('  routing_valid=False')

        except (ValueError, OSError) as exc:
            print(f'  status=ERROR ({exc})')

if __name__ == '__main__':
    p = argparse.ArgumentParser(description='Opt-in save routing test; only while games stopped')
    sub = p.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('prepare')
    a.add_argument('--game', required=True)
    a.add_argument('--namespace', required=True)
    a.add_argument('--save-id', default=None)
    b = sub.add_parser('disable')
    b.add_argument('--game', required=True)
    sub.add_parser('inspect')
    args = p.parse_args()
    if args.cmd == 'prepare':
        print(json.dumps(prepare(args.game, args.namespace, args.save_id), indent=2))
    elif args.cmd == 'disable':
        print(json.dumps(disable(args.game), indent=2))
    else:
        inspect()


def _remove_route_only(game, *, require_missing_game):
    _valid(game)
    config = _read_config()
    entry = config.get(game)
    if entry is None:
        return False
    if not isinstance(entry, dict) or not isinstance(entry.get('namespace'), str):
        raise ValueError('Malformed managed routing entry')
    namespace = _valid(entry['namespace'])
    directory = GAMES / game
    if require_missing_game and (directory.exists() or directory.is_symlink()):
        raise ValueError('Cannot retire route while game directory exists')
    # Protect against a stale config accidentally reused by another live game.
    for other, route in config.items():
        if other != game and isinstance(route, dict) and route.get('namespace') == namespace:
            raise ValueError('Save namespace is assigned to multiple games')
    del config[game]
    _write_config(config)
    return True


def retire_uninstalled_route(game):
    return _remove_route_only(game, require_missing_game=True)


def retire_orphaned_route(game, *, expected_directory=None):
    if expected_directory is not None and Path(expected_directory) != GAMES / game:
        raise ValueError('Unexpected import destination')
    from catalog import connect
    with connect() as db:
        count = db.execute(
            'SELECT COUNT(*) FROM installations WHERE directory_name=?', (game,)
        ).fetchone()[0]
        if count:
            raise ValueError('Cannot retire route while catalog record exists')
    return _remove_route_only(game, require_missing_game=True)

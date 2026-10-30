from save_routing import GAMES, SAVES, _read_config


def verify_uninstall(game):
    local = GAMES / game / 'game' / 'saves'
    route = _read_config().get(game)
    if local.is_symlink():
        if not route:
            raise ValueError('Unrecognized game-local save symlink; uninstall blocked')
        root = SAVES / route['namespace']
        target = root / 'current'
        if (not target.is_dir() or target.is_symlink() or
                local.resolve() != target.resolve() or
                not (root / 'renpy-parent').is_dir()):
            raise ValueError('Unexpected managed save routing; uninstall blocked')
    elif local.exists():
        if not local.is_dir() or any(local.iterdir()):
            raise ValueError('Unprotected game-local saves detected; uninstall blocked')
    original = GAMES / game / 'game' / 'saves.storyburrow-original'
    if original.exists() and not route:
        raise ValueError('Unprotected game-local backup detected; uninstall blocked')

#!/usr/bin/env python3
import json
import os
import re
import signal
import socketserver
import subprocess
import threading
import uuid
import time
from pathlib import Path

GAMES = Path(os.environ.get('STORYBURROW_GAMES_DIR', '/games')).resolve()
SOCKET = Path(os.environ.get('STORYBURROW_MANAGER_SOCKET', '/tmp/runtime-ubuntu/storyburrow-manager.sock'))
LOG_DIR = Path(os.environ.get('STORYBURROW_GAME_LOG_DIR', '/logs/games'))
VALID_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._ -]{0,127}$')
WINDOW_LINE = re.compile(r'^\s*(0x[0-9a-fA-F]+)\s+\"([^\"]*)\".*?\s+(\d+)x(\d+)[+-]')
WINDOW_FIT_TIMEOUT = 20.0
WINDOW_STABILIZE_SECONDS = 8.0
WINDOW_STABILIZE_INTERVAL = 0.5
STOP_GRACE_SECONDS = 4.0



def find_game(name):
    if not isinstance(name, str) or not VALID_NAME.fullmatch(name) or name in ('.', '..'):
        raise ValueError('Invalid game directory name')
    directory = (GAMES / name).resolve()
    if directory.parent != GAMES or not directory.is_dir():
        raise ValueError('Game not found in /games')
    if not (directory / 'game').is_dir():
        raise ValueError('Not a Ren\'Py game directory (missing game/)')
    launchers = sorted(p for p in directory.glob('*.sh') if p.is_file() and not p.is_symlink())
    preferred = directory / f'{name}.sh'
    if preferred in launchers:
        return directory, preferred
    if len(launchers) == 1:
        return directory, launchers[0]
    if not launchers:
        raise ValueError('No Linux .sh launcher found')
    raise ValueError('Multiple launchers found; cannot choose safely')



def x11_windows(env):
    result = subprocess.run(
        ['xwininfo', '-root', '-tree'], env=env, capture_output=True,
        text=True, check=True, timeout=3,
    )
    windows = {}
    for line in result.stdout.splitlines():
        match = WINDOW_LINE.match(line)
        if not match:
            continue
        window_id, title, width, height = match.groups()
        if title and int(width) >= 200 and int(height) >= 150:
            windows[window_id] = title
    return windows


def desktop_size(env):
    result = subprocess.run(
        ['xrandr', '--current'], env=env, capture_output=True,
        text=True, check=True, timeout=3,
    )
    match = re.search(r'\bcurrent (\d+) x (\d+)\b', result.stdout)
    if not match:
        raise RuntimeError('Could not read X11 desktop size')
    return int(match.group(1)), int(match.group(2))


def window_geometry(window_id, env):
    result = subprocess.run(
        ['xwininfo', '-id', window_id], env=env, capture_output=True,
        text=True, check=True, timeout=3,
    )
    values = {}
    for label, key in [('Width', 'width'), ('Height', 'height'),
                       ('Absolute upper-left X', 'x'), ('Absolute upper-left Y', 'y')]:
        match = re.search(r'^\s*' + re.escape(label) + r':\s*(-?\d+)\s*$',
                          result.stdout, re.MULTILINE)
        if not match:
            raise RuntimeError(f'Could not read window {label}')
        values[key] = int(match.group(1))
    return (values['width'], values['height'], values['x'], values['y'])


def fit_game_window(proc, name, existing_windows, env):
    try:
        deadline = time.monotonic() + WINDOW_FIT_TIMEOUT
        previous = None
        stable_checks = 0
        candidate = None
        while time.monotonic() < deadline and proc.poll() is None:
            windows = x11_windows(env)
            fresh = {wid: title for wid, title in windows.items()
                     if wid not in existing_windows}
            # A splash screen may appear briefly: require a stable unique window.
            if len(fresh) == 1:
                current = next(iter(fresh))
                if current == previous:
                    stable_checks += 1
                else:
                    previous, stable_checks = current, 1
                if stable_checks >= 3:
                    candidate = current
                    break
            else:
                previous, stable_checks = None, 0
            time.sleep(0.4)

        if candidate is None or proc.poll() is not None:
            if proc.poll() is None:
                print(f'[StoryBurrow] Window fitting skipped for {name}: '
                      'no unambiguous new game window found', flush=True)
            return

        width, height = desktop_size(env)
        expected = (width, height, 0, 0)
        stabilize_until = time.monotonic() + WINDOW_STABILIZE_SECONDS
        adjustments = 0
        while time.monotonic() < stabilize_until and proc.poll() is None:
            actual = window_geometry(candidate, env)
            if actual != expected:
                subprocess.run(['xdotool', 'windowsize', candidate, str(width), str(height)],
                               env=env, check=True, capture_output=True, timeout=3)
                subprocess.run(['xdotool', 'windowmove', candidate, '0', '0'],
                               env=env, check=True, capture_output=True, timeout=3)
                adjustments += 1
                print(f'[StoryBurrow] Fitted {name} window {candidate} '
                      f'from {actual[0]}x{actual[1]}+{actual[2]}+{actual[3]} '
                      f'to {width}x{height}+0+0 (adjustment {adjustments})', flush=True)
            time.sleep(WINDOW_STABILIZE_INTERVAL)
        if proc.poll() is None:
            print(f'[StoryBurrow] Window stabilization finished for {name}; '
                  f'adjustments={adjustments}', flush=True)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print(f'[StoryBurrow] Window fitting unavailable for {name}: {exc}', flush=True)


class Session:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.game = None
        self.last_game = None
        self.last_exit_code = None
        self.stopping_pid = None
        self.maintenance = None

    def status(self):
        with self.lock:
            p = self.process
            running = p is not None and p.poll() is None
            return {'running': running, 'game': self.game if running else None,
                    'pid': p.pid if running else None, 'last_game': self.last_game,
                    'last_exit_code': self.last_exit_code}

    def list_games(self):
        result = []
        if GAMES.is_dir():
            for path in sorted(GAMES.iterdir()):
                if not path.is_dir():
                    continue
                try:
                    _, launcher = find_game(path.name)
                    result.append({'id': path.name, 'launcher': launcher.name})
                except ValueError:
                    continue
        return {'games': result}

    def reserve_update(self, name):
        with self.lock:
            if self.maintenance is not None:
                return {'ok': False, 'error': 'Another game update is already active'}
            if self.process is not None and self.process.poll() is None:
                return {'ok': False, 'error': 'Stop the game before updating'}
            try:
                find_game(name)
            except ValueError as exc:
                return {'ok': False, 'error': str(exc)}
            self.maintenance = name
            return {'ok': True}

    def release_update(self, name):
        with self.lock:
            if self.maintenance != name:
                return {'ok': False, 'error': 'No matching update reservation'}
            self.maintenance = None
            return {'ok': True}

    def replace_update(self, name, pending, backup):
        with self.lock:
            if self.maintenance != name:
                return {'ok': False, 'error': 'No update reservation'}
            if self.process is not None and self.process.poll() is None:
                return {'ok': False, 'error': 'A game is running'}
            old = GAMES / name
            staged = Path(pending)
            saved = Path(backup)
            backups = GAMES / '.storyburrow-update-backups'
            try:
                if (not staged.is_dir() or staged.is_symlink() or
                    staged.resolve().parent != (GAMES / '.storyburrow-installing').resolve() or
                    saved.parent != backups or backups.is_symlink() or
                    saved.exists() or saved.is_symlink() or
                    not old.is_dir() or old.is_symlink()):
                    raise ValueError('Invalid update swap paths')
                # Keep the original installation on the same /games mount.
                os.rename(old, saved)
                try:
                    os.rename(staged, old)
                except Exception:
                    os.rename(saved, old)
                    raise
                return {'ok': True}
            except (OSError, ValueError) as exc:
                return {'ok': False, 'error': str(exc)}

    def rollback_update(self, name, backup):
        with self.lock:
            if self.maintenance != name:
                return {'ok': False, 'error': 'No update reservation'}
            previous = Path(backup)
            backups = GAMES / '.storyburrow-update-backups'
            active = GAMES / name
            quarantine = backups / uuid.uuid4().hex
            if (previous.parent != backups or previous.is_symlink() or
                not previous.is_dir() or active.is_symlink() or not active.is_dir()):
                return {'ok': False, 'error': 'Invalid rollback paths'}
            try:
                os.rename(active, quarantine)
                try:
                    os.rename(previous, active)
                except Exception:
                    os.rename(quarantine, active)
                    raise
                return {'ok': True, 'failed_build': str(quarantine)}
            except OSError as exc:
                return {'ok': False, 'error': str(exc)}

    def retire(self, name):
        with self.lock:
            if self.maintenance is not None:
                return {'ok': False, 'error': 'An update is in progress'}
            if self.process is not None and self.process.poll() is None:
                return {'ok': False, 'error': 'Stop the current game before uninstalling'}
            try:
                directory, _ = find_game(name)
                if directory.is_symlink():
                    raise ValueError('Symlink installation is not removable')
                trash_root = GAMES / '.storyburrow-uninstall-trash'
                if trash_root.is_symlink():
                    raise ValueError('Uninstall trash is a symlink')
                trash_root.mkdir(mode=0o700, exist_ok=True)
                token = uuid.uuid4().hex
                os.rename(directory, trash_root / token)
                return {'ok': True, 'trash_token': token}
            except (OSError, ValueError) as exc:
                return {'ok': False, 'error': str(exc)}

    def restore(self, name, token):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return {'ok': False, 'error': 'Cannot restore while a game runs'}
            if not isinstance(name, str) or not VALID_NAME.fullmatch(name) or name in ('.', '..'):
                return {'ok': False, 'error': 'Invalid game name'}
            if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
                return {'ok': False, 'error': 'Invalid trash token'}
            trash_root = GAMES / '.storyburrow-uninstall-trash'
            origin, target = trash_root / token, GAMES / name
            if trash_root.is_symlink() or origin.is_symlink() or not origin.is_dir() or target.exists():
                return {'ok': False, 'error': 'Restore destination unavailable'}
            try:
                os.rename(origin, target)
                return {'ok': True}
            except OSError as exc:
                return {'ok': False, 'error': str(exc)}

    def start(self, name):
        with self.lock:
            if self.maintenance is not None:
                return {'ok': False, 'error': 'Game updates are in progress'}
            if self.process is not None and self.process.poll() is None:
                return {'ok': False, 'error': f'Already running: {self.game}'}
            try:
                directory, launcher = find_game(name)
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                logpath = LOG_DIR / f'{name}.log'
                env = os.environ.copy()
                env.setdefault('SDL_VIDEODRIVER', 'x11')

                try:
                    from save_routing import _read_config, SAVES as ROUTE_SAVES

                    route = _read_config().get(name)

                    if route is not None:
                        namespace = route['namespace']
                        root = ROUTE_SAVES / namespace
                        current = root / 'current'
                        parent = root / 'renpy-parent'
                        local = directory / 'game' / 'saves'

                        if (not current.is_dir()
                                or not local.is_symlink()
                                or local.resolve() != current.resolve()
                                or not parent.is_dir()):
                            raise ValueError(
                                'Managed save routing is not prepared correctly'
                            )

                        env['RENPY_PATH_TO_SAVES'] = str(parent)

                        print(
                            f'[StoryBurrow] Managed saves active for {name}: {root}',
                            flush=True
                        )

                    else:
                        # Temporary legacy routing until this game is migrated.
                        env['RENPY_PATH_TO_SAVES'] = '/saves'

                except (OSError, ValueError, KeyError) as exc:
                    raise ValueError(f'Managed save routing check failed: {exc}')

                try:
                    existing_windows = x11_windows(env)
                except (OSError, subprocess.SubprocessError) as exc:
                    print(f'[StoryBurrow] Could not snapshot X11 windows: {exc}', flush=True)
                    existing_windows = None
                with logpath.open('ab', buffering=0) as logfile:
                    proc = subprocess.Popen(['/bin/bash', str(launcher)], cwd=directory,
                                            env=env, stdin=subprocess.DEVNULL,
                                            stdout=logfile, stderr=subprocess.STDOUT,
                                            start_new_session=True)
            except (ValueError, OSError) as exc:
                return {'ok': False, 'error': str(exc)}
            self.process = proc
            self.game = name
            self.last_game = name
            self.last_exit_code = None
            self.stopping_pid = None
            threading.Thread(target=self._reap, args=(proc,), daemon=True).start()
            if existing_windows is not None:
                threading.Thread(target=fit_game_window,
                                 args=(proc, name, existing_windows, env),
                                 daemon=True).start()
            return {'ok': True, 'game': name, 'pid': proc.pid, 'log': str(logpath)}

    def _reap(self, proc):
        code = proc.wait()
        with self.lock:
            if self.process is proc:
                self.last_exit_code = code
                self.last_game = self.game
                self.process = None
                self.game = None
                self.stopping_pid = None
        print(f'[StoryBurrow] Game exited pid={proc.pid} code={code}', flush=True)

    def _finish_stop(self, proc, name):
        try:
            proc.wait(timeout=STOP_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        with self.lock:
            if self.process is not proc or proc.poll() is not None:
                return
            print(f'[StoryBurrow] {name} ignored SIGTERM; forcing process group stop', flush=True)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def stop(self):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                return {'ok': False, 'error': 'No running game'}
            proc = self.process
            name = self.game
            if self.stopping_pid == proc.pid:
                return {'ok': True, 'stopping': name}
            self.stopping_pid = proc.pid
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            threading.Thread(target=self._finish_stop,
                             args=(proc, name), daemon=True).start()
            return {'ok': True, 'stopping': name}

    def shutdown(self):
        with self.lock:
            proc = self.process
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()


SESSION = Session()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            raw = self.rfile.readline(8193)
            if len(raw) > 8192:
                raise ValueError('Request too large')
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError('Expected JSON object')
            action = msg.get('action')
            if action == 'status':
                result = {'ok': True, **SESSION.status()}
            elif action == 'list':
                result = {'ok': True, **SESSION.list_games()}
            elif action == 'start':
                result = SESSION.start(msg.get('game'))
            elif action == 'stop':
                result = SESSION.stop()
            elif action == 'reserve_update':
                result = SESSION.reserve_update(msg.get('game'))
            elif action == 'release_update':
                result = SESSION.release_update(msg.get('game'))
            elif action == 'replace_update':
                result = SESSION.replace_update(msg.get('game'), msg.get('pending'), msg.get('backup'))
            elif action == 'rollback_update':
                result = SESSION.rollback_update(msg.get('game'), msg.get('backup'))
            elif action == 'retire':
                result = SESSION.retire(msg.get('game'))
            elif action == 'restore':
                result = SESSION.restore(msg.get('game'), msg.get('trash_token'))
            else:
                result = {'ok': False, 'error': 'Unknown action'}
        except (ValueError, json.JSONDecodeError) as exc:
            result = {'ok': False, 'error': str(exc)}
        self.wfile.write((json.dumps(result) + '\n').encode())


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main():
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET.exists():
        SOCKET.unlink() 
    with Server(str(SOCKET), Handler) as server:
        os.chmod(SOCKET, 0o600)
        print(f'[StoryBurrow] Game manager listening on {SOCKET}', flush=True)
        def terminate(_signum, _frame):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            SESSION.shutdown()
            SOCKET.unlink(missing_ok=True)


if __name__ == '__main__':
    main()

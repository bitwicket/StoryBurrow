#!/usr/bin/env python3
# CLI for the local StoryBurrow game manager.
import json
import os
import socket
import sys

SOCKET = os.environ.get('STORYBURROW_MANAGER_SOCKET', '/tmp/runtime-ubuntu/storyburrow-manager.sock')


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ('list', 'status', 'start', 'stop') or (args[0] == 'start') != (len(args) == 2) or (args[0] != 'start' and len(args) != 1):
        print('Usage: gamectl.py list | status | start GAME_DIRECTORY | stop', file=sys.stderr)
        return 2
    message = {'action': args[0]}
    if args[0] == 'start':
        message['game'] = args[1]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(10)
            conn.connect(SOCKET)
            conn.sendall((json.dumps(message) + '\n').encode())
            response = b''
            while not response.endswith(b'\n'):
                chunk = conn.recv(8192)
                if not chunk:
                    break
                response += chunk
        result = json.loads(response)
    except (OSError, ValueError) as exc:
        print(f'Game manager unavailable: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())

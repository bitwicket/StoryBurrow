import http.client
import ipaddress
import json
import os
import secrets
import socket
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from flask import Blueprint, jsonify, request, session
from importer.inspector import InspectionError, inspect
from import_routes import (
    ROOT, LOCK, MAX_UPLOAD, cleanup, get_upload,
    require_csrf, save_state, problem
)

bp = Blueprint('import_urls', __name__)

MAX_REDIRECTS = 5
BLOCK_SIZE = 1024 * 1024
MAX_SECONDS = 2 * 3600

class DownloadCancelled(Exception):
    pass

class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, port, address):
        super().__init__(
            hostname, port=port, timeout=30,
            context=ssl.create_default_context()
        )
        self.address = address
    def connect(self):
        sock = socket.create_connection(
            (self.address, self.port), self.timeout
        )
        try:
            self.sock = self._context.wrap_socket(
                sock, server_hostname=self.host
            )
        except Exception:
            sock.close()
            raise

def validated_address(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port or 443
        host = parsed.hostname
    except ValueError:
        raise ValueError('Invalid download URL')
    if (
        parsed.scheme != 'https'
        or not host
        or parsed.username
        or parsed.password
        or port != 443
    ):
        raise ValueError('Only public HTTPS URLs on port 443 are supported')
    try:
        addresses = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        raise ValueError('Could not resolve download hostname')
    if not addresses:
        raise ValueError('Download hostname has no addresses')
    ips = []
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if not ip.is_global:
            raise ValueError(
                'Download URL resolves to a private or reserved address'
            )
        ips.append(str(ip))
    return parsed, port, ips[0]

def open_download(url, extra_headers=None):
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed, port, address = validated_address(current)
        conn = PinnedConnection(parsed.hostname, port, address)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        try:
            headers = {
                'Accept': 'application/zip,application/octet-stream,*/*',
                'User-Agent': 'StoryBurrow/0.9',
            }
            if extra_headers:
                host = (parsed.hostname or '').lower()
                if host == 'gofile.io' or host.endswith('.gofile.io'):
                    headers.update(extra_headers)
            conn.request('GET', path, headers=headers)
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                conn.close()
                if not location:
                    raise ValueError('Download redirect has no destination')
                current = urljoin(current, location)
                if len(current) > 8192:
                    raise ValueError('Redirect URL is too long')
                continue
            if response.status != 200:
                conn.close()
                raise ValueError(
                    f'Download server returned HTTP {response.status}'
                )
            return conn, response
        except Exception:
            conn.close()
            raise
    raise ValueError('Too many download redirects')

def update(token, **changes):
    with LOCK:
        root = ROOT / token
        if not (root / 'state.json').exists():
            return False
        state = json.loads((root / 'state.json').read_text('utf-8'))
        state.update(changes)
        save_state(root, state)
        return True

def cancelled(token):
    with LOCK:
        path = ROOT / token / 'state.json'
        if not path.exists():
            return True
        state = json.loads(path.read_text('utf-8'))
        return bool(state.get('cancel_requested'))

def download_worker(token, url):
    root = ROOT / token
    destination = root / 'upload.zip'
    connection = None
    try:
        started = time.monotonic()
        connection, response = open_download(url)
        length = response.getheader('Content-Length')
        total = int(length) if length and length.isdecimal() else 0
        if total > MAX_UPLOAD:
            raise ValueError('Download exceeds the 20 GiB import limit')
        update(token, total=total)
        received = 0
        last_report = 0
        with destination.open('xb') as output:
            while True:
                if cancelled(token):
                    raise DownloadCancelled()
                if time.monotonic() - started > MAX_SECONDS:
                    raise ValueError('Download exceeded the two-hour limit')
                chunk = response.read(BLOCK_SIZE)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_UPLOAD:
                    raise ValueError('Download exceeds the 20 GiB import limit')
                output.write(chunk)
                now = time.monotonic()
                if now - last_report >= 0.5:
                    update(token, received=received, offsets=[received])
                    last_report = now
        if total and received != total:
            raise ValueError('Download was incomplete')
        if cancelled(token):
            raise DownloadCancelled()
        update(
            token,
            received=received,
            offsets=[received],
            total=received,
            files=[{'path': 'download.zip', 'size': received}],
            status='inspecting'
        )
        inspected = inspect(destination)
        if cancelled(token):
            raise DownloadCancelled()
        inspected['source']['name'] = 'download.zip'
        inspected['preview_id'] = token
        (root / 'inspection.json').write_text(
            json.dumps(inspected), encoding='utf-8'
        )
        update(token, status='ready')
    except DownloadCancelled:
        destination.unlink(missing_ok=True)
        update(token, status='cancelled', error='Download cancelled')
    except (OSError, ValueError, InspectionError, ssl.SSLError,
            TimeoutError, http.client.HTTPException) as exc:
        destination.unlink(missing_ok=True)
        update(token, status='failed', error=str(exc)[:300])
    except Exception:
        destination.unlink(missing_ok=True)
        update(
            token, status='failed',
            error='Unexpected download failure. Check server logs.'
        )
    finally:
        if connection:
            connection.close()

@bp.post('/api/import/url')
def start_url_download():
    require_csrf()
    data = request.get_json(silent=True) or {}
    url = data.get('url', '')
    if not isinstance(url, str) or not (1 <= len(url) <= 8192):
        return problem('Provide a valid download URL')
    from import_hosted import provider_for
    provider = provider_for(url)
    try:
        validated_address(url)
    except ValueError as exc:
        return problem(str(exc))
    with LOCK:
        cleanup()
        token = secrets.token_hex(16)
        root = ROOT / token
        root.mkdir(mode=0o700)
        state = {
            'owner': session['user'],
            'kind': 'zip',
            'files': [{'path': 'download.zip', 'size': 0}],
            'total': 0,
            'offsets': [0],
            'received': 0,
            'status': 'downloading',
            'cancel_requested': False,
            'error': None
        }
        save_state(root, state)
    if provider:
        from import_hosted import hosted_worker
        worker = hosted_worker
        worker_args = (token, url, provider)
    else:
        worker = download_worker
        worker_args = (token, url)
    threading.Thread(
        target=worker,
        args=worker_args,
        daemon=True,
        name=f'storyburrow-url-{token[:8]}'
    ).start()
    return jsonify(ok=True, upload_id=token, status='downloading'), 202

@bp.get('/api/import/url/<token>')
def url_status(token):
    with LOCK:
        root, state = get_upload(token)
        result = {
            'ok': True,
            'upload_id': token,
            'status': state['status'],
            'received_bytes': state['received'],
            'total_bytes': state['total'],
            'error': state.get('error')
        }
        if state['status'] == 'ready':
            result['inspection'] = json.loads(
                (root / 'inspection.json').read_text('utf-8')
            )
        return jsonify(result)

@bp.post('/api/import/url/<token>/cancel')
def cancel_url_download(token):
    require_csrf()
    with LOCK:
        root, state = get_upload(token)
        if state['status'] in ('installing', 'installed'):
            return problem('Installation cannot be cancelled here', 409)
        if state['status'] in ('downloading', 'inspecting'):
            state['cancel_requested'] = True
            save_state(root, state)
        return jsonify(ok=True)
import http.client
import re
import zipfile
from email.message import Message
import ipaddress
import json
import os
import secrets
import socket
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote
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

PATCH_MAX = 256 * 1024 * 1024

# Never interpret an HTML host landing page, access denial, or a JSON API
# response as a patch. Opaque download URLs are common, so the URL path is
# not evidence that a payload contains a real standalone file.
STANDALONE_EXTENSIONS = {
    '.rpy', '.rpyc', '.rpym', '.rpymc', '.rpa', '.txt', '.json',
    '.png', '.jpg', '.jpeg', '.webp', '.svg', '.ogg', '.mp3',
    '.wav', '.webm', '.mp4', '.ttf', '.otf', '.ini', '.cfg',
    '.py', '.sh', '.dll', '.so', '.bin', '.patch', '.diff',
}

def _response_filename(disposition):
    """Parse RFC 5987/quoted Content-Disposition filenames, if supplied."""
    if not disposition:
        return None
    message = Message()
    message['content-disposition'] = disposition
    value = message.get_filename()
    if isinstance(value, tuple):
        from email.utils import collapse_rfc2231_value
        value = collapse_rfc2231_value(value)
    if not value:
        return None
    return Path(str(value).replace('\\', '/')).name[:200] or None

def _looks_like_webpage(path, content_type=''):
    # Only inspect the first bytes; reading an archive in full is unnecessary.
    with path.open('rb') as payload:
        head = payload.read(4096).lstrip(b'\xef\xbb\xbf\x00\x09\x0a\x0d\x20')
    if any(term in content_type.lower() for term in (
        'text/html', 'application/xhtml', 'application/json', 'text/xml'
    )):
        return True
    lower = head[:512].lower()
    return (lower.startswith((b'<!doctype html', b'<html', b'<?xml', b'{"error"', b'{"message"'))
            or b'<html' in lower[:128])

def _patch_source_name(path, source_name, content_type='', explicit_destination=None):
    name = Path((source_name or '').replace('\\', '/')).name[:200] or 'download'
    if _looks_like_webpage(path, content_type):
        raise ValueError(
            'The URL returned a webpage or API response, not a patch file. '
            'Use the direct file download link (not a share/preview page).'
        )
    with path.open('rb') as payload:
        signature = payload.read(4)
    if zipfile.is_zipfile(path):
        if not name.lower().endswith('.zip'):
            name += '.zip'
        return name
    if signature.startswith(b'PK\x03\x04') or name.lower().endswith('.zip'):
        raise ValueError('Downloaded file is not a valid ZIP archive')
    if not explicit_destination and Path(name).suffix.lower() not in STANDALONE_EXTENSIONS:
        raise ValueError(
            'The download is not a ZIP and has no recognizable file extension. '
            'Provide a standalone destination such as game/patch.rpy, '
            'or use a direct ZIP download URL.'
        )
    if explicit_destination and not Path(name).suffix:
        name = Path(explicit_destination).name
    return name

def download_limit(token):
    with LOCK:
        state = json.loads((ROOT / token / 'state.json').read_text('utf-8'))
    return PATCH_MAX if state.get('purpose') == 'patch' else MAX_UPLOAD

def finish_download(token, source_name):
    root = ROOT / token
    destination = root / 'upload.zip'
    with LOCK:
        state = json.loads((root / 'state.json').read_text('utf-8'))
    if state.get('purpose') != 'patch':
        inspected = inspect(destination)
        if cancelled(token):
            raise DownloadCancelled()
        inspected['source']['name'] = source_name
        inspected['preview_id'] = token
        (root / 'inspection.json').write_text(json.dumps(inspected), encoding='utf-8')
        update(token, status='ready')
        return
    if cancelled(token):
        raise DownloadCancelled()
    from werkzeug.datastructures import FileStorage
    from patcher import create_stage, preview
    from main import GAMES
    game_id = state['game_id']
    game_dir = GAMES / game_id
    # The game was checked when the download began. Check again in case it was removed.
    if game_dir.is_symlink() or not game_dir.is_dir() or game_dir.resolve().parent != GAMES:
        raise ValueError('Target game is no longer installed')
    source_name = _patch_source_name(destination, source_name,
                                     state.get('content_type', ''),
                                     state.get('destination'))
    with destination.open('rb') as stream:
        uploaded = FileStorage(stream=stream, filename=source_name)
        patch_token, patch_state = create_stage(
            game_id, state['owner'], uploaded, game_dir, state.get('destination')
        )
    if cancelled(token):
        import shutil
        shutil.rmtree(__import__('patcher').ROOT / 'staging' / patch_token, ignore_errors=True)
        raise DownloadCancelled()
    update(token, status='ready', patch_token=patch_token,
           patch_preview=preview(game_dir, patch_state),
           stripped=patch_state.get('stripped'))
    destination.unlink(missing_ok=True)

def download_worker(token, url):
    root = ROOT / token
    destination = root / 'upload.zip'
    connection = None
    try:
        started = time.monotonic()
        connection, response = open_download(url)
        length = response.getheader('Content-Length')
        total = int(length) if length and length.isdecimal() else 0
        limit = download_limit(token)
        if total > limit:
            raise ValueError('Download exceeds the size limit')
        update(token, total=total,
               content_type=response.getheader('Content-Type', ''),
               response_filename=_response_filename(response.getheader('Content-Disposition')))
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
                if received > limit:
                    raise ValueError('Download exceeds the size limit')
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
        name = unquote(urlsplit(url).path.rsplit('/', 1)[-1]) or 'download.zip'
        # Limit filename to a safe display name, not a filesystem path.
        name = Path(name.replace('\\', '/')).name[:200] or 'download.zip'
        if download_limit(token) == PATCH_MAX:
            with LOCK:
                metadata = json.loads((root / 'state.json').read_text('utf-8'))
            name = metadata.get('response_filename') or name
        finish_download(token, name if download_limit(token) == PATCH_MAX else 'download.zip')
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

def begin_download(url, owner, purpose='game', game_id=None, destination=None):
    from import_hosted import provider_for
    provider = provider_for(url)
    validated_address(url)
    with LOCK:
        cleanup()
        token = secrets.token_hex(16)
        root = ROOT / token
        root.mkdir(mode=0o700)
        state = {
            'owner': owner,
            'purpose': purpose,
            'game_id': game_id,
            'destination': destination,
            'kind': 'zip',
            'files': [{'path': 'download.zip', 'size': 0}],
            'total': 0, 'offsets': [0], 'received': 0,
            'status': 'downloading', 'cancel_requested': False, 'error': None
        }
        save_state(root, state)
    if provider:
        from import_hosted import hosted_worker
        worker, args = hosted_worker, (token, url, provider)
    else:
        worker, args = download_worker, (token, url)
    threading.Thread(target=worker, args=args, daemon=True,
                     name=f'storyburrow-url-{token[:8]}').start()
    return token

@bp.post('/api/import/url')
def start_url_download():
    require_csrf()
    data = request.get_json(silent=True) or {}
    url = data.get('url', '')
    if not isinstance(url, str) or not (1 <= len(url) <= 8192):
        return problem('Provide a valid download URL')
    try:
        token = begin_download(url, session['user'])
    except ValueError as exc:
        return problem(str(exc))
    return jsonify(ok=True, upload_id=token, status='downloading'), 202

@bp.get('/api/import/url/<token>')
def url_status(token):
    with LOCK:
        root, state = get_upload(token)
        if state.get('purpose') == 'patch':
            return problem('Use the patch download status endpoint', 404)
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



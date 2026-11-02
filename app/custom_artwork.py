import http.client
import io
import ipaddress
import os
import secrets
import socket
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, abort, redirect, render_template, request, session, url_for
from PIL import Image, ImageOps, UnidentifiedImageError

bp = Blueprint('custom_art', __name__)

ROOT = Path(os.getenv('STORYBURROW_CUSTOM_ART_DIR', '/data/artwork-overrides'))
MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 25_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

def path_for(game_id):
    return ROOT / game_id / 'cover.png'

def current_override(game_id):
    if not game_id or game_id in ('.', '..') or '/' in game_id or '\\' in game_id:
        return None
    directory = ROOT / game_id
    path = directory / 'cover.png'
    if directory.is_symlink() or path.is_symlink():
        return None
    return path if path.is_file() else None

def _authorize(game_id):
    from main import installed_games, manager_command
    token = request.form.get('csrf_token', '')
    if not secrets.compare_digest(token, session.get('_csrf', 'not-set')):
        abort(400, 'Invalid form token')
    games, error = installed_games()
    if error:
        abort(503, 'Game manager unavailable')
    if game_id not in {game['id'] for game in games}:
        abort(404)
    status = manager_command('status')
    if not status.get('ok'):
        abort(503, 'Game manager unavailable')
    if status.get('running'):
        abort(409, 'Stop the current game before editing artwork')

def _normalize(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('Image must be 12 MB or smaller')
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format not in ('PNG', 'JPEG', 'WEBP', 'AVIF'):
                raise ValueError('Only PNG, JPEG, WebP, and AVIF images are supported')
            if img.width * img.height > MAX_PIXELS:
                raise ValueError('Image dimensions are too large')
            img = ImageOps.exif_transpose(img)
            img.thumbnail((1600, 2400), Image.Resampling.LANCZOS)
            img = img.convert('RGBA' if 'A' in img.getbands() else 'RGB')
            output = io.BytesIO()
            img.save(output, format='PNG', optimize=True)
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError('Invalid or unsupported image') from exc

def _save(game_id, raw):
    image = _normalize(raw)
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = ROOT / game_id
    if directory.is_symlink():
        raise ValueError('Invalid artwork directory')
    directory.mkdir(mode=0o700, exist_ok=True)
    target = directory / 'cover.png'
    if target.is_symlink():
        raise ValueError('Invalid artwork destination')
    temporary = directory / ('.cover-' + secrets.token_hex(12) + '.tmp')
    try:
        with temporary.open('xb') as file:
            file.write(image)
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname, address):
        super().__init__(hostname, port=443, timeout=15,
                         context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(
                sock, server_hostname=self.host
            )
        except Exception:
            sock.close()
            raise

def _download(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname
            or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError('Provide a direct HTTPS image URL')
    hostname = parsed.hostname.rstrip('.')
    try:
        addresses = socket.getaddrinfo(
            hostname, 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise ValueError('Image hostname could not be resolved') from exc
    public_addresses = []
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if not ip.is_global:
            raise ValueError('Image URL resolves to a non-public address')
        public_addresses.append(str(ip))
    if not public_addresses:
        raise ValueError('Image hostname has no usable addresses')
    path = parsed.path or '/'
    if parsed.query:
        path += '?' + parsed.query
    connection = PinnedHTTPSConnection(hostname, public_addresses[0])
    try:
        connection.request('GET', path, headers={
            'Host': hostname,
            'Accept': 'image/png,image/jpeg,image/webp,image/jpg,image/avif',
            'User-Agent': 'StoryBurrow/0.9'
        })
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError(
                f'Image server returned HTTP {response.status}; '
                'use a direct image URL'
            )
        content_length = response.getheader('Content-Length')
        if content_length and int(content_length) > MAX_BYTES:
            raise ValueError('Remote image exceeds 12 MB')
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Remote image exceeds 12 MB')
        return raw
    finally:
        connection.close()

@bp.post('/games/<game_id>/artwork/custom')
def set_custom_artwork(game_id):
    _authorize(game_id)
    try:
        source = request.form.get('source')
        if source == 'file':
            upload = request.files.get('image')
            if not upload or not upload.filename:
                raise ValueError('Choose an image file')
            raw = upload.stream.read(MAX_BYTES + 1)
        elif source == 'url':
            url = request.form.get('image_url', '').strip()
            if len(url) > 2048:
                raise ValueError('Image URL is too long')
            raw = _download(url)
        else:
            raise ValueError('Choose an artwork source')
        _save(game_id, raw)
    except (ValueError, OSError, ssl.SSLError, TimeoutError) as exc:
        return render_template(
            'message.html',
            title='Artwork could not be saved',
            message=str(exc)
        ), 422
    return redirect(url_for('edits.edit_game', game_id=game_id), code=303)

@bp.post('/games/<game_id>/artwork/custom/remove')
def remove_custom_artwork(game_id):
    _authorize(game_id)
    path = current_override(game_id)
    if path:
        path.unlink()
    return redirect(url_for('edits.edit_game', game_id=game_id), code=303)
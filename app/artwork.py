import json
import logging
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from catalog import connect, initialize

LOG = logging.getLogger('storyburrow.artwork')
ROOT = Path(os.environ.get('STORYBURROW_ARTWORK_DIR', '/data/artwork'))
API = 'https://api.vndb.org/kana/vn'
MAX_IMAGE = 10 * 1024 * 1024
VN_ID = re.compile(r'^v[1-9][0-9]*$')


class ArtworkError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ArtworkError('Redirected artwork URLs are not accepted')


def fetch_image_url(vndb_id, opener=None):
    if not isinstance(vndb_id, str) or not VN_ID.fullmatch(vndb_id):
        raise ArtworkError('Invalid VNDB visual novel ID')
    opener = opener or build_opener(NoRedirect)
    payload = json.dumps({'filters': ['id', '=', vndb_id], 'fields': 'image.url', 'results': 1}).encode()
    req = Request(API, data=payload, headers={'Content-Type':'application/json', 'User-Agent':'StoryBurrow/0.8'}, method='POST')
    with opener.open(req, timeout=12) as response:
        data = response.read(256 * 1024 + 1)
    if len(data) > 256 * 1024:
        raise ArtworkError('VNDB response was too large')
    results = json.loads(data).get('results') or []
    if len(results) != 1 or results[0].get('id') != vndb_id:
        raise ArtworkError('VNDB record not found')
    image = results[0].get('image') or {}
    url = image.get('url')
    if not isinstance(url, str) or not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme != 'https' or not parsed.hostname or not (parsed.hostname == 'vndb.org' or parsed.hostname.endswith('.vndb.org')) or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ArtworkError('Unexpected VNDB image host')
    return url


def image_type(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp'
    raise ArtworkError('Unsupported image format (expected JPEG, PNG, or WebP)')


def cache_vndb_cover(game_id, *, opener=None):
    initialize()
    with connect() as db:
        game = db.execute('SELECT game_id,vndb_id FROM games WHERE game_id=?', (game_id,)).fetchone()
        if game is None:
            raise ArtworkError('Game not found in catalog')
        if not game['vndb_id']:
            return None
        existing = db.execute('''SELECT storage_path FROM game_assets WHERE game_id=? AND asset_type='cover'
                                 AND source_type='user' ORDER BY created_at DESC LIMIT 1''', (game_id,)).fetchone()
        if existing and existing['storage_path'] and Path(existing['storage_path']).is_file():
            return Path(existing['storage_path'])
        vndb_id = game['vndb_id']
    opener = opener or build_opener(NoRedirect)
    url = fetch_image_url(vndb_id, opener)
    if not url:
        return None
    req = Request(url, headers={'User-Agent': 'StoryBurrow/0.8', 'Accept':'image/jpeg,image/png,image/webp'})
    with opener.open(req, timeout=20) as response:
        if response.headers.get('Content-Length') and int(response.headers['Content-Length']) > MAX_IMAGE:
            raise ArtworkError('Cover exceeds 10 MiB limit')
        data = response.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE:
        raise ArtworkError('Cover exceeds 10 MiB limit')
    extension = image_type(data)
    ROOT.mkdir(parents=True, exist_ok=True)
    path = ROOT / f'{game_id}.{extension}'
    temp = ROOT / f'.{game_id}.{uuid.uuid4().hex}.tmp'
    try:
        temp.write_bytes(data)
        os.replace(temp, path)
        with connect() as db:
            # Ensure catalog still points to same VNDB match before attaching art.
            latest = db.execute('SELECT vndb_id FROM games WHERE game_id=?', (game_id,)).fetchone()
            if not latest or latest['vndb_id'] != vndb_id:
                path.unlink(missing_ok=True)
                raise ArtworkError('VNDB selection changed during download')
            db.execute("DELETE FROM game_assets WHERE game_id=? AND asset_type='cover' AND source_type='vndb'", (game_id,))
            db.execute('''INSERT INTO game_assets(asset_id,game_id,asset_type,storage_path,source_url,source_type,created_at)
                          VALUES (?,?,?,?,?,?,?)''', (str(uuid.uuid4()), game_id, 'cover', str(path), url, 'vndb', int(time.time())))
    finally:
        temp.unlink(missing_ok=True)
    return path


def cover_for_directory(directory_name):
    try:
        with connect() as db:
            rows = db.execute('''SELECT a.storage_path FROM installations i
              JOIN game_assets a ON a.game_id=i.game_id
              WHERE i.directory_name=? AND i.status='installed' AND a.asset_type='cover'
              ORDER BY CASE a.source_type WHEN 'user' THEN 0 WHEN 'vndb' THEN 1 ELSE 2 END, a.created_at DESC''',
              (directory_name,)).fetchall()
        base = ROOT.resolve()
        for row in rows:
            candidate = Path(row['storage_path'] or '').resolve()
            if candidate.is_file() and candidate.parent == base and not candidate.is_symlink():
                return candidate
    except (sqlite3.Error, OSError, ValueError) as exc:
        LOG.warning('Catalog artwork unavailable: %s', exc)
    return None


def cache_all_missing():
    initialize()
    with connect() as db:
        rows = db.execute("SELECT game_id,display_title FROM games WHERE vndb_id IS NOT NULL").fetchall()
    for row in rows:
        if cover_for_game_id(row['game_id']):
            print(f"SKIP {row['display_title']}: cached cover exists", flush=True)
            continue
        try:
            result = cache_vndb_cover(row['game_id'])
            print(f"{'OK' if result else 'NO COVER'} {row['display_title']}: {result or ''}", flush=True)
        except Exception as exc:
            print(f"ERROR {row['display_title']}: {exc}", flush=True)


def cover_for_game_id(game_id):
    with connect() as db:
        row = db.execute("SELECT directory_name FROM installations WHERE game_id=? AND status='installed' LIMIT 1", (game_id,)).fetchone()
    return cover_for_directory(row['directory_name']) if row else None


if __name__ == '__main__':
    cache_all_missing()

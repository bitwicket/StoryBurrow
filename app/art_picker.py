import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, build_opener

from artwork import ArtworkError, NoRedirect, ROOT, MAX_IMAGE, image_type
from catalog import connect, initialize

VNDB_API = 'https://api.vndb.org/kana/'
VN_ID = re.compile(r'^v[1-9]\d*$')
IMAGE_FIELDS = 'id,url,thumbnail,sexual,violence,dims'
MAX_RESULTS = 120


def explicit_allowed():
    initialize()
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        row = db.execute("SELECT value FROM app_settings WHERE key='vndb_allow_explicit'").fetchone()
    return bool(row and row['value'] == '1')


def set_explicit_allowed(allowed):
    initialize()
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute("INSERT INTO app_settings(key,value) VALUES('vndb_allow_explicit',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if allowed else '0',))


def _query(endpoint, payload):
    req = Request(VNDB_API + endpoint, data=json.dumps(payload).encode(), method='POST',
                  headers={'Content-Type': 'application/json', 'User-Agent': 'StoryBurrow/0.8'})
    with build_opener(NoRedirect).open(req, timeout=16) as response:
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ArtworkError('VNDB response too large')
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ArtworkError('Invalid VNDB response')
    return result


def _safe_url(url):
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and parsed.hostname is not None and
            (parsed.hostname == 'vndb.org' or parsed.hostname.endswith('.vndb.org')) and
            not parsed.username and not parsed.password and parsed.port in (None, 443))


def _entry(image, kind, label):
    if not isinstance(image, dict):
        return None
    url = image.get('url')
    if not _safe_url(url):
        return None
    preview = image.get('thumbnail') if _safe_url(image.get('thumbnail')) else url
    image_id = image.get('id')
    if not isinstance(image_id, str):
        return None
    return {'id': image_id, 'url': url, 'thumbnail': preview, 'kind': kind,
            'label': label, 'sexual': image.get('sexual') or 0,
            'violence': image.get('violence') or 0}


def choices(vndb_id, allow_explicit=None):
    if not isinstance(vndb_id, str) or not VN_ID.fullmatch(vndb_id):
        raise ArtworkError('Invalid VNDB ID')
    if allow_explicit is None:
        allow_explicit = explicit_allowed()
    # VNDB VN result provides primary image and screenshots. Releases are queried separately.
    image_fields = ','.join('image.' + f for f in IMAGE_FIELDS.split(','))
    screenshot_fields = ','.join('screenshots.' + f for f in IMAGE_FIELDS.split(','))
    data = _query('vn', {'filters': ['id', '=', vndb_id], 'fields': image_fields + ',' + screenshot_fields, 'results': 1})
    results = data.get('results') or []
    if len(results) != 1 or results[0].get('id') != vndb_id:
        raise ArtworkError('VNDB entry not found')
    vn = results[0]
    items = []
    primary = _entry(vn.get('image'), 'primary', 'Primary cover')
    if primary:
        items.append(primary)
    for ix, shot in enumerate(vn.get('screenshots') or [], 1):
        item = _entry(shot, 'screenshot', f'Screenshot {ix}')
        if item:
            items.append(item)
    release_fields = ','.join('images.' + f for f in IMAGE_FIELDS.split(',')) + ',images.type,title'
    try:
        release = _query('release', {'filters': ['vn', '=', ['id', '=', vndb_id]],
                                     'fields': release_fields, 'results': 30})
    except (ArtworkError, OSError, ValueError):
        release = {}  # Release artwork is optional; do not hide primary/screenshot choices.
    for rel in release.get('results') or []:
        for img in rel.get('images') or []:
            item = _entry(img, 'release', 'Release artwork')
            if item:
                items.append(item)
    deduped = {}
    for item in items:
        deduped.setdefault(item['id'], item)
    visible = [item for item in deduped.values() if allow_explicit or float(item['sexual']) < 1.0]
    return visible[:MAX_RESULTS]


def save_choice(game_id, selected_id, *, allow_explicit=None):
    initialize()
    with connect() as db:
        row = db.execute('SELECT vndb_id FROM games WHERE game_id=?', (game_id,)).fetchone()
        if not row or not row['vndb_id']:
            raise ArtworkError('No VNDB match for this game')
        vn_id = row['vndb_id']
    available = choices(vn_id, allow_explicit)
    choice = next((item for item in available if item['id'] == selected_id), None)
    if choice is None:
        raise ArtworkError('Artwork not found or not allowed by current content setting')
    req = Request(choice['url'], headers={'User-Agent': 'StoryBurrow/0.8', 'Accept': 'image/jpeg,image/png,image/webp'})
    with build_opener(NoRedirect).open(req, timeout=20) as response:
        length = response.headers.get('Content-Length')
        if length and int(length) > MAX_IMAGE:
            raise ArtworkError('Artwork exceeds 10 MiB')
        raw = response.read(MAX_IMAGE + 1)
    if len(raw) > MAX_IMAGE:
        raise ArtworkError('Artwork exceeds 10 MiB')
    ext = image_type(raw)
    ROOT.mkdir(parents=True, exist_ok=True)
    name = f'{game_id}-{uuid.uuid4().hex}.{ext}'
    target = ROOT / name
    temp = ROOT / ('.' + name + '.tmp')
    try:
        temp.write_bytes(raw)
        os.replace(temp, target)
        with connect() as db:
            current = db.execute('SELECT vndb_id FROM games WHERE game_id=?', (game_id,)).fetchone()
            if not current or current['vndb_id'] != vn_id:
                raise ArtworkError('Game metadata changed during artwork download')
            db.execute("DELETE FROM game_assets WHERE game_id=? AND asset_type='cover'", (game_id,))
            db.execute('''INSERT INTO game_assets(asset_id,game_id,asset_type,storage_path,source_url,source_type,created_at)
                          VALUES (?,?,?,?,?,?,?)''', (str(uuid.uuid4()), game_id, 'cover', str(target), choice['url'], 'user', int(time.time())))
        return str(target)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)

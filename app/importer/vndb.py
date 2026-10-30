import json
import os
import urllib.error
import urllib.request

ENDPOINT = 'https://api.vndb.org/kana/vn'
FIELDS = 'title,alttitle,description,released,developers.name,image.url,image.thumbnail,image.sexual,image.violence,rating,olang,aliases'


class VNDBError(RuntimeError):
    pass


def search_vn(title, token=None, limit=8):
    title = title.strip()
    if not 2 <= len(title) <= 150:
        raise VNDBError('Search title must be between 2 and 150 characters')
    payload = json.dumps({'filters': ['search', '=', title],
                          'fields': FIELDS, 'results': max(1, min(int(limit), 20))}).encode()
    headers = {'Content-Type': 'application/json', 'User-Agent': 'StoryBurrow/0.8 (local metadata review)'}
    token = token or os.environ.get('STORYBURROW_VNDB_TOKEN', '')
    if token:
        headers['Authorization'] = 'Token ' + token
    request = urllib.request.Request(ENDPOINT, data=payload, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            data = json.load(response)
    except (urllib.error.URLError, ValueError) as exc:
        raise VNDBError(f'VNDB lookup failed: {exc}') from exc
    results = data.get('results', [])
    # Return candidates for review. User accepts match on import modal
    return [{'id': item.get('id'), 'title': item.get('title'), 'alttitle': item.get('alttitle'),
             'description': item.get('description'), 'released': item.get('released'),
             'developers': [d.get('name') for d in item.get('developers', [])],
             'image': item.get('image'), 'rating': item.get('rating'),
             'language': item.get('olang'), 'aliases': item.get('aliases', [])}
            for item in results]

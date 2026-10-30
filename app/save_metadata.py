import json
import math
import struct
import zipfile
from pathlib import Path

MAX_SAVE_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 128 * 1024
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
MAX_ZIP_MEMBERS = 128
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


def _bounded_member(zf, name, maximum):
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > maximum or info.compress_size > MAX_SAVE_BYTES:
        raise ValueError('Oversized save member')
    with zf.open(info) as stream:
        content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise ValueError('Oversized save member')
    return content


def read_metadata(path):
    result = {'label': None, 'version': None, 'runtime': None,
              'created': None, 'engine': None, 'thumbnail': False,
              'metadata_status': 'Unavailable'}
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_SAVE_BYTES:
            return result
        with zipfile.ZipFile(path, 'r') as zf:
            if len(zf.infolist()) > MAX_ZIP_MEMBERS:
                raise ValueError('Too many save members')
            raw = _bounded_member(zf, 'json', MAX_JSON_BYTES)
            if raw:
                obj = json.loads(raw.decode('utf-8'))
                if isinstance(obj, dict):
                    for key, field in (('_save_name','label'),('_version','version')):
                        value = obj.get(key)
                        if isinstance(value, str):
                            result[field] = value[:160]
                    runtime = obj.get('_game_runtime')
                    if isinstance(runtime, (int, float)) and not isinstance(runtime, bool) and math.isfinite(runtime) and 0 <= runtime <= 1e10:
                        hours, rem = divmod(int(runtime), 3600)
                        result['runtime'] = f'{hours}h {rem // 60}m' if hours else f'{rem // 60}m'
                    created = obj.get('_ctime')
                    if isinstance(created, (int, float)) and math.isfinite(created) and 0 < created < 4102444800:
                        from save_time import local_time
                        result['created'] = local_time(created)
                    engine = obj.get('_renpy_version')
                    if isinstance(engine, list) and all(type(x) is int for x in engine[:5]):
                        result['engine'] = '.'.join(str(x) for x in engine[:5])[:80]
                    result['metadata_status'] = 'Available'
            try:
                info = zf.getinfo('screenshot.png')
                result['thumbnail'] = 0 < info.file_size <= MAX_SCREENSHOT_BYTES
            except KeyError:
                pass
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, UnicodeError, json.JSONDecodeError):
        result['metadata_status'] = 'Unreadable or unsupported'
    return result


def read_screenshot(path):
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_SAVE_BYTES:
        raise ValueError('Invalid save file')
    with zipfile.ZipFile(path) as zf:
        if len(zf.infolist()) > MAX_ZIP_MEMBERS:
            raise ValueError('Too many save members')
        data = _bounded_member(zf, 'screenshot.png', MAX_SCREENSHOT_BYTES)
    if data is None or len(data) < 24 or not data.startswith(PNG_MAGIC):
        raise ValueError('Screenshot unavailable')
    width, height = struct.unpack('>II', data[16:24])
    if not (1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= 20_000_000):
        raise ValueError('Invalid screenshot dimensions')
    return data

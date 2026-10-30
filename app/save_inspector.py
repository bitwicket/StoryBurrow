from io import BytesIO
from pathlib import Path
import pickle
import re
import struct
import zlib

MAX_INDEX = 32 * 1024 * 1024
MAX_SCRIPT = 16 * 1024 * 1024
MAX_DECOMPRESSED = 32 * 1024 * 1024
VALID_ID = re.compile(r'^[^/\\\x00-\x1f]{1,180}$')
SOURCE = re.compile(r'(?m)^\s*(?:(?:define|default)\s+)?config\.save_directory\s*=\s*[\"\']([^\"\'\r\n]+)[\"\']')
COMPILED = re.compile(rb'(?:store\.config).{0,160}?save_directory.{0,160}?["\']([^"\'\x00\r\n]{1,180})["\']', re.DOTALL)


class _IndexUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise pickle.UnpicklingError('RPA index cannot construct objects')


def _safe_id(value):
    if not isinstance(value, str) or not VALID_ID.fullmatch(value) or value in ('.', '..'):
        return None
    return value


def _from_source(data):
    # Source search is deliberately limited to plain quoted literal assignments.
    text = data.decode('utf-8', 'replace')
    matches = {_safe_id(x) for x in SOURCE.findall(text)} - {None}
    return next(iter(matches)) if len(matches) == 1 else None


def _decompress_limited(blob):
    obj = zlib.decompressobj()
    result = obj.decompress(blob, MAX_DECOMPRESSED + 1)
    if len(result) > MAX_DECOMPRESSED or obj.unconsumed_tail:
        raise ValueError('Decompressed script too large')
    result += obj.flush()
    if len(result) > MAX_DECOMPRESSED or not obj.eof:
        raise ValueError('Invalid compressed script')
    return result


def _from_compiled(data):
    if not data.startswith(b'RENPY RPC2'):
        return None
    matches = set()
    position = 10
    while position + 12 <= len(data):
        slot, start, length = struct.unpack_from('<III', data, position)
        if slot == 0:
            break
        if not length or length > MAX_SCRIPT or start + length > len(data):
            return None
        decoded = _decompress_limited(data[start:start + length])
        for raw in COMPILED.findall(decoded):
            try:
                text = raw.decode('utf-8')
            except UnicodeDecodeError:
                continue
            result = _safe_id(text)
            if result:
                matches.add(result)
        position += 12
    return next(iter(matches)) if len(matches) == 1 else None


def _rpa_entry(path, name='options.rpyc'):
    if path.stat().st_size < 34:
        return None
    with path.open('rb') as archive:
        header = archive.readline(128).decode('ascii', 'replace').strip().split()
        if len(header) != 3 or header[0] != 'RPA-3.0':
            return None
        offset, key = int(header[1], 16), int(header[2], 16)
        size = path.stat().st_size
        if not 0 < offset < size or size - offset > MAX_INDEX:
            return None
        archive.seek(offset)
        index_blob = _decompress_limited(archive.read())
        index = _IndexUnpickler(BytesIO(index_blob)).load()
        if not isinstance(index, dict):
            return None
        chunks = index.get(name)
        if not isinstance(chunks, (list, tuple)):
            return None
        result = bytearray()
        for chunk in chunks:
            if not isinstance(chunk, (list, tuple)) or len(chunk) not in (2, 3):
                return None
            start, length = chunk[0] ^ key, chunk[1] ^ key
            prefix = chunk[2] if len(chunk) == 3 else b''
            if not isinstance(prefix, bytes) or start < 0 or length < 0 or start + length > size:
                return None
            if len(result) + len(prefix) + length > MAX_SCRIPT:
                return None
            result.extend(prefix)
            archive.seek(start)
            data = archive.read(length)
            if len(data) != length:
                return None
            result.extend(data)
        return bytes(result)


def inspect_save_directory(game_dir):
    unknown = {'save_directory': None, 'save_directory_source': None,
               'save_directory_confidence': 'unknown'}
    try:
        game = Path(game_dir) / 'game'
        if not game.is_dir():
            return unknown
        source_matches = set()
        for path in game.rglob('*.rpy'):
            if path.is_symlink() or path.stat().st_size > MAX_SCRIPT:
                continue
            identifier = _from_source(path.read_bytes())
            if identifier:
                source_matches.add(identifier)
        if len(source_matches) > 1:
            return unknown
        if source_matches:
            return {'save_directory': source_matches.pop(),
                    'save_directory_source': 'source_script', 'save_directory_confidence': 'confirmed'}
        compiled_matches = set()
        loose = game / 'options.rpyc'
        if loose.is_file() and not loose.is_symlink() and loose.stat().st_size <= MAX_SCRIPT:
            value = _from_compiled(loose.read_bytes())
            if value:
                compiled_matches.add(value)
        archives = sorted(game.glob('*.rpa'), key=lambda p: ('scripts' not in p.name.lower(), p.name))
        for archive in archives:
            if archive.is_symlink():
                continue
            data = _rpa_entry(archive)
            if data:
                value = _from_compiled(data)
                if value:
                    compiled_matches.add(value)
        if len(compiled_matches) == 1:
            return {'save_directory': compiled_matches.pop(),
                    'save_directory_source': 'compiled_script',
                    'save_directory_confidence': 'inferred'}
    except (OSError, ValueError, TypeError, OverflowError, EOFError, pickle.UnpicklingError, zlib.error, struct.error):
        pass
    return unknown

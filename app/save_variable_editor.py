import base64
import json
import math
import os
import pickletools
import struct
import zipfile
from pathlib import Path
from inspect_variables import read_variables

class EditError(ValueError):
    pass

KEY_PATH = Path(os.environ.get('STORYBURROW_SAVE_TOKEN_KEYS', '/saves/tokens/security_keys.txt'))
MAX_LOG = 32 * 1024 * 1024
LITERAL_OPS = {'NONE','NEWTRUE','NEWFALSE','BININT','BININT1','BININT2','LONG1','LONG4','BINFLOAT','BINUNICODE','SHORT_BINUNICODE','BINUNICODE8'}

def _encoded(value):
    if value is None: return b'N'
    if type(value) is bool: return b'\x88' if value else b'\x89'
    if type(value) is int:
        if 0 <= value <= 255: return b'K' + bytes([value])
        if 0 <= value <= 65535: return b'M' + struct.pack('<H',value)
        if -2147483648 <= value <= 2147483647: return b'J' + struct.pack('<i',value)
        raise EditError('Only 32-bit integers supported for now')
    if type(value) is float:
        if not math.isfinite(value): raise EditError('Non-finite float')
        return b'G' + struct.pack('>d',value)
    if type(value) is str:
        blob=value.encode('utf8')
        if len(blob)>4096: raise EditError('String exceeds 4096 bytes')
        return b'X'+struct.pack('<I',len(blob))+blob
    raise EditError('Unsupported value type')

def parse_value(kind, raw):
    if kind == 'bool':
        if raw not in ('true','false'):raise EditError('Select true or false')
        return raw=='true'
    if kind == 'int':
        if not raw or len(raw)>30:raise EditError('Invalid integer')
        try: return int(raw)
        except ValueError: raise EditError('Invalid integer')
    if kind == 'float':
        try: value=float(raw)
        except ValueError: raise EditError('Invalid float')
        if not math.isfinite(value):raise EditError('Invalid float')
        return value
    if kind == 'str':
        if len(raw.encode('utf8'))>4096:raise EditError('String too long')
        return raw
    raise EditError('Unsupported variable type')

def rewrite_log(log, variable, expected, value):
    if len(log)>MAX_LOG:raise EditError('Save state too large')
    before=read_variables(log)['primitive_variables']
    if variable not in before:raise EditError('Variable is not an inspectable primitive')
    original=before[variable]
    if original['value'] != expected or type(expected).__name__ != original['type']:
        raise EditError('Save changed since editor opened; reload it')
    if type(value).__name__ != original['type']:
        raise EditError('Variable type cannot be changed')
    ops=list(pickletools.genops(log))
    matches=[i for i, (op,arg,_) in enumerate(ops) if op.name in ('BINUNICODE','SHORT_BINUNICODE','BINUNICODE8') and arg == variable]
    if len(matches)!=1:raise EditError('Variable is ambiguous in serialized state')
    idx=matches[0]+1
    # Key may be added to pickle memo immediately after its string opcode.
    while idx<len(ops) and ops[idx][0].name in ('BINPUT','LONG_BINPUT','PUT','MEMOIZE'):
        idx+=1
    if idx>=len(ops) or ops[idx][0].name not in LITERAL_OPS:
        raise EditError('Variable uses a memo reference or unsupported encoding')
    op,arg,start=ops[idx]
    if type(arg).__name__ != original['type'] or arg != expected:
        # The literal is not the currently inspected value.
        raise EditError('Serialized literal does not match inspected variable')
    end=ops[idx+1][2] if idx+1<len(ops) else len(log)
    fresh=log[:start]+_encoded(value)+log[end:]
    after=read_variables(fresh)['primitive_variables']
    expected_after={k:dict(v) for k,v in before.items()}
    expected_after[variable]={'type':original['type'],'value':value}
    if after!=expected_after:raise EditError('Unexpected state changes; refusing edit')
    final=list(pickletools.genops(fresh))
    if not final or final[-1][0].name!='STOP' or final[-1][2]!=len(fresh)-1:
        raise EditError('Serialized stream did not validate')
    return fresh

def _sign(log):
    # Requires `ecdsa` installed in the container. Keys are never sent to the browser.
    try:
        from ecdsa import SigningKey
    except ImportError as e:
        raise EditError('Signing requires the ecdsa Python package') from e
    if not KEY_PATH.is_file() or KEY_PATH.is_symlink():
        raise EditError(f'RenPy signing key unavailable: {KEY_PATH}')
    lines=[]
    for line in KEY_PATH.read_text().splitlines():
        parts=line.split()
        if len(parts)>=2 and parts[0]=='signing-key':
            try: key=SigningKey.from_der(base64.b64decode(parts[1],validate=True))
            except Exception as e:raise EditError('Invalid signing key') from e
            sig=key.sign(log)
            public=key.verifying_key.to_der()
            if not key.verifying_key.verify(sig,log):raise EditError('Signature verification failed')
            lines.append('signature '+base64.b64encode(public).decode()+' '+base64.b64encode(sig).decode()+'\n')
    if not lines:raise EditError('No signing-key found in RenPy token file')
    return ''.join(lines).encode('utf8')

def make_edit(source, variable, expected, value, destination):
    source=Path(source);destination=Path(destination)
    if not source.is_file() or source.is_symlink() or destination.exists():raise EditError('Unsafe edit paths')
    with zipfile.ZipFile(source) as zin:
        members=zin.infolist()
        names=[m.filename for m in members]
        if len(members)>64 or len(names)!=len(set(names)) or 'log' not in names:
            raise EditError('Unsupported archive layout')
        if any(m.file_size>MAX_LOG for m in members):raise EditError('Oversized archive member')
        log=zin.read('log')
        fresh=rewrite_log(log,variable,expected,value)
        signatures=_sign(fresh) if 'signatures' in names else None
        with zipfile.ZipFile(destination,'w') as zout:
            for info in members:
                data=fresh if info.filename=='log' else signatures if info.filename=='signatures' else zin.read(info)
                zout.writestr(info,data)
    with zipfile.ZipFile(destination) as z:
        if z.testzip() is not None or z.read('log')!=fresh:raise EditError('Archive verification failed')
    return {'signed': signatures is not None, 'before': expected, 'after': value}

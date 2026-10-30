#!/usr/bin/env python3
import argparse
import json
import pickletools
import re
import zipfile
from pathlib import Path

class Opaque:
    __slots__ = ('description',)
    def __init__(self, description='complex object'): self.description = description

MARK = object()

class InspectionError(Exception): pass

def read_variables(payload, max_bytes=32 * 1024 * 1024, max_ops=2_000_000):
    if len(payload) > max_bytes: raise InspectionError('Serialized state exceeds inspection limit')
    stack = []
    memo = {}
    root = None
    count = 0
    def collapse():
        try: i = len(stack) - 1 - stack[::-1].index(MARK)
        except ValueError: raise InspectionError('Malformed pickle mark')
        values = stack[i+1:]
        del stack[i:]
        return values
    def pair_update(target, values):
        if len(values) % 2: raise InspectionError('Invalid dictionary entries')
        # Only the root dict records entries; nested mutable objects are opaque.
        if target is root:
            for i in range(0,len(values),2):
                k,v = values[i:i+2]
                if isinstance(k,str) and k.startswith('store.'):
                    target[k] = v
    for op,arg,pos in pickletools.genops(payload):
        count += 1
        if count > max_ops: raise InspectionError('Too many pickle operations')
        n = op.name
        if n in ('PROTO','FRAME'): pass
        elif n == 'MARK': stack.append(MARK)
        elif n in ('NONE','NEWTRUE','NEWFALSE'): stack.append({'NONE':None,'NEWTRUE':True,'NEWFALSE':False}[n])
        elif n in ('BININT','BININT1','BININT2','LONG','LONG1','LONG4','BINFLOAT','FLOAT','INT'):
            stack.append(arg)
        elif n in ('BINUNICODE','SHORT_BINUNICODE','BINUNICODE8','UNICODE','SHORT_BINSTRING','BINSTRING','STRING'):
            stack.append(arg if isinstance(arg,str) else Opaque('binary string'))
        elif n == 'EMPTY_DICT':
            value = {} if root is None else Opaque('dictionary')
            if root is None: root = value
            stack.append(value)
        elif n in ('EMPTY_LIST','EMPTY_TUPLE','EMPTY_SET'): stack.append(Opaque(n.lower()))
        elif n in ('GLOBAL','STACK_GLOBAL','EXT1','EXT2','EXT4','PERSID','BINPERSID'):
            if n == 'STACK_GLOBAL':
                stack.pop();stack.pop()
            elif n == 'BINPERSID': stack.pop()
            stack.append(Opaque('class or callable'))
        elif n in ('BINPUT','LONG_BINPUT','PUT'): memo[int(arg)] = stack[-1]
        elif n in ('BINGET','LONG_BINGET','GET'):
            if int(arg) not in memo: raise InspectionError('Missing memo reference')
            stack.append(memo[int(arg)])
        elif n == 'MEMOIZE': memo[len(memo)] = stack[-1]
        elif n == 'POP': stack.pop()
        elif n == 'POP_MARK': collapse()
        elif n == 'DUP': stack.append(stack[-1])
        elif n in ('TUPLE1','TUPLE2','TUPLE3'):
            del stack[-int(n[-1]):]
            stack.append(Opaque('tuple'))
        elif n in ('LIST','TUPLE','DICT','FROZENSET'):
            collapse(); stack.append(Opaque(n.lower()))
        elif n == 'NEWOBJ':
            stack.pop();stack.pop();stack.append(Opaque('instance'))
        elif n == 'NEWOBJ_EX':
            stack.pop();stack.pop();stack.pop();stack.append(Opaque('instance'))
        elif n == 'REDUCE':
            stack.pop();stack.pop();stack.append(Opaque('reduced object'))
        elif n == 'BUILD': stack.pop()  # leave object
        elif n == 'SETITEM':
            value=stack.pop();key=stack.pop();target=stack[-1]
            pair_update(target,[key,value])
        elif n == 'SETITEMS':
            values=collapse();target=stack[-1];pair_update(target,values)
        elif n == 'APPEND': stack.pop()
        elif n == 'APPENDS': collapse()
        elif n == 'ADDITEMS': collapse()
        elif n == 'OBJ': collapse(); stack.append(Opaque('object'))
        elif n == 'INST': collapse(); stack.append(Opaque('instance'))
        elif n == 'STOP': break
        else: raise InspectionError('Unsupported pickle opcode: '+n)
    if root is None: raise InspectionError('Top-level dictionary not found')
    primitive = {}
    unsupported = []
    for key,val in root.items():
        if val is None or type(val) in (bool,int,float,str):
            # Avoid non-finite float values breaking JSON interoperability.
            if type(val) is float and not __import__('math').isfinite(val): unsupported.append(key)
            else: primitive[key]={'type':type(val).__name__,'value':val}
        else: unsupported.append(key)
    return {'primitive_variables':primitive,'unsupported_variables':sorted(unsupported), 'top_level_store_entries':len(root),'pickle_operations':count,'interpretation':'top-level assignments in serialized log; rollback objects not reconstructed'}

def inspect(path):
    if not path.is_file() or path.is_symlink() or path.suffix != '.save': raise InspectionError('Expected a regular .save file')
    with zipfile.ZipFile(path) as z:
        info = z.getinfo('log')
        if info.file_size > 32*1024*1024: raise InspectionError('log member too large')
        payload = z.read(info)
    result = read_variables(payload)
    result['file']=path.name
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('file',type=Path)
    p.add_argument('--search',default='')
    p.add_argument('--limit',type=int,default=100)
    a=p.parse_args()
    result=inspect(a.file)
    selected={k:v for k,v in result.pop('primitive_variables').items() if a.search.lower() in k.lower()}
    result['matching_variables']=len(selected)
    result['variables']=dict(list(sorted(selected.items()))[:max(0,min(a.limit,1000))])
    print(json.dumps(result,indent=2,ensure_ascii=False))
if __name__=='__main__': main()

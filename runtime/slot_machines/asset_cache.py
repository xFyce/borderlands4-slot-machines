"""Shared authored data; the worker reads files and never accesses Unreal."""
import gzip
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_data={}
_pending={}
_reader=ThreadPoolExecutor(max_workers=1,thread_name_prefix='BL4-slot-data')

def _read(path):
    if path.suffix=='.gz':
        with gzip.open(path,'rt',encoding='utf-8') as source:data=json.load(source)
        if path.name=='glow_geometry.json.gz':
            expected=data['source_file_sha']
            if hashlib.sha256((path.parent/'geometry.json').read_bytes()).hexdigest()!=expected:
                raise RuntimeError('Prepared lights do not match the installed cabinet geometry')
        return data
    return json.loads(path.read_text(encoding='utf-8'))

def preload(root):
    for name in ('geometry.json','reels.json','glow_geometry.json.gz','animations.json.gz'):
        path=root/name;key=str(path.resolve())
        if key not in _data and key not in _pending:_pending[key]=_reader.submit(_read,path)

def ready(root):
    folder=root.resolve()
    return all(future.done() for key,future in _pending.items()
               if Path(key).parent==folder)

def load_json(path):
    key=str(path.resolve())
    if key not in _data:
        future=_pending.pop(key,None)
        _data[key]=future.result() if future is not None else _read(path)
    return _data[key]

def clear():
    _data.clear()
    for future in _pending.values():future.cancel()
    _pending.clear()

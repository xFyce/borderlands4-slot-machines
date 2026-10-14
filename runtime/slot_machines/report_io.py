"""File writes only: no game objects or mutable reports cross to this worker."""
from concurrent.futures import ThreadPoolExecutor
_writer=ThreadPoolExecutor(max_workers=1,thread_name_prefix='BL4-slot-reports')

def _write(path,payload):
    try:path.write_text(payload,encoding='utf-8')
    except Exception as error:print('[Slots Preview] Background diagnostic failed: '+str(error))

def submit(path,payload):
    return _writer.submit(_write,path,payload)

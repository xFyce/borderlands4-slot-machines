"""Bounded native-call breadcrumbs; never dereference Unreal objects here."""
import mmap
import os
import time
from pathlib import Path
from . import diagnostics

SLOTS=256
WIDTH=256

class CrashTrace:
    def __init__(self,folder):
        self.folder=Path(folder);self.file=None;self.mapping=None
        self.sequence=0;self.failed=False;self.context='initialization'
    def record(self,event):
        if not diagnostics.enabled or self.failed:return
        try:
            if self.mapping is None:
                self.folder.mkdir(parents=True,exist_ok=True)
                # Unique file per process/session; earlier crash evidence survives.
                path=self.folder/f'slots_native_trace_{os.getpid()}_{time.time_ns()}.log'
                self.file=path.open('w+b');self.file.truncate(SLOTS*WIDTH)
                self.mapping=mmap.mmap(self.file.fileno(),SLOTS*WIDTH)
            self.sequence+=1
            text=f'{self.sequence:012d} {time.monotonic():.6f} {self.context} | {event}'
            data=text.encode('utf-8',errors='replace')[:WIDTH-1].ljust(WIDTH-1,b' ')+b'\n'
            offset=((self.sequence-1)%SLOTS)*WIDTH
            self.mapping[offset:offset+WIDTH]=data
        except Exception:
            self.failed=True
    def phase(self,name):
        self.context=name;self.record('phase')
    def close(self):
        if self.mapping is not None:
            self.mapping.close();self.mapping=None
        if self.file is not None:
            self.file.close();self.file=None

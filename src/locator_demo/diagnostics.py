"""Bounded, thread-safe local diagnostic journals; no image/audio content."""
from collections import Counter,deque
from contextvars import ContextVar
from datetime import datetime,timedelta,timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading
import time
import uuid


request_id=ContextVar('diagnostic_request_id',default='')


class DiagnosticJournal:
    def __init__(self,directory=None):
        self.directory=Path(directory).resolve() if directory else None
        self.session_id=uuid.uuid4().hex[:12]
        self._lock=threading.RLock();self._handlers={};self._closed=False
        self.recent=deque(maxlen=200);self.failures=Counter();self.write_error=''
        if self.directory:
            try:
                self.directory.mkdir(parents=True,exist_ok=True)
                for kind in ('runtime','serial'):
                    handler=RotatingFileHandler(self.directory/f'{kind}.jsonl',maxBytes=5*1024*1024,
                                                backupCount=4,encoding='utf-8',delay=True)
                    handler.setFormatter(logging.Formatter('%(message)s'));self._handlers[kind]=handler
            except OSError as exc:self.write_error=str(exc)

    def record(self,event,detail=None,*,kind='runtime',level='info',timestamp=None):
        now=time.time() if timestamp is None else float(timestamp)
        row=dict(ts=now,time=datetime.fromtimestamp(now,timezone(timedelta(hours=8))).isoformat(),
                 session_id=self.session_id,request_id=request_id.get(),event=str(event),level=level,detail=detail or {})
        with self._lock:
            if self._closed:return row
            if kind=='runtime':self.recent.append(row)
            if level=='error':self.failures[str(event)]+=1
            handler=self._handlers.get(kind)
            if handler:
                try:
                    message=json.dumps(row,ensure_ascii=False,separators=(',',':'),default=str)
                    handler.handle(logging.LogRecord('deepsight',logging.INFO,'',0,message,(),None))
                except Exception as exc:self.write_error=str(exc)
        return row

    def snapshot(self):
        with self._lock:
            failures=[row for row in self.recent if row['level']=='error']
            return dict(enabled=bool(self._handlers),directory=str(self.directory) if self.directory else None,
                        session_id=self.session_id,error_counts=dict(self.failures),last_failure=failures[-1] if failures else None,
                        write_error=self.write_error,rotation='5 MiB per file, four backups per journal',
                        content='events, bounded serial frames and timing; no camera pixels or audio samples')

    def path(self,kind):
        if kind not in ('runtime','serial') or self.directory is None:return None
        return self.directory/f'{kind}.jsonl'

    def close(self):
        with self._lock:
            self._closed=True
            for handler in self._handlers.values():handler.close()

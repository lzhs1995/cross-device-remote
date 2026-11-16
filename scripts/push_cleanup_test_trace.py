"""Bounded test-only stage I/O trace; no retry or decision changes."""
from contextlib import ExitStack
import os
from pathlib import Path
import threading
from unittest.mock import patch


STAT_FIELDS = ('st_mode', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns',
               'st_ctime_ns', 'st_nlink', 'st_file_attributes', 'st_reparse_tag')


class StageCleanupTrace:
    def __init__(self, module):
        self.module = module
        self.local = threading.local()
        self.stack = ExitStack()

    def begin(self, stage):
        self.local.stage = os.fspath(stage)
        self.local.events = []
        self.local.fds = set()
        self.local.dropped = 0

    def snapshot(self):
        return {'events': list(self.local.events), 'dropped': self.local.dropped}

    def _record(self, operation, **values):
        events = self.local.events
        if len(events) < 48:
            events.append(dict(sequence=len(events), operation=operation, **values))
        else:
            self.local.dropped += 1

    def _is_path(self, value):
        return hasattr(self.local, 'stage') and isinstance(value, (str, bytes, os.PathLike)) and os.fspath(value) == self.local.stage

    def _is_fd(self, value):
        return hasattr(self.local, 'fds') and value in self.local.fds

    def _call(self, operation, function, args, kwargs, *, is_stat=False):
        try:
            value = function(*args, **kwargs)
        except OSError as error:
            self._record(operation, outcome='ERROR', error_type=type(error).__name__,
                         errno=error.errno, winerror=getattr(error, 'winerror', None))
            raise
        values = {'outcome': 'OK'}
        if is_stat:
            values['stat'] = {key: getattr(value, key, None) for key in STAT_FIELDS}
        elif operation == 'read':
            values['read_bytes'] = len(value)
        self._record(operation, **values)
        return value

    def __enter__(self):
        original_lstat, original_open = self.module.os.lstat, self.module._open_recovered_stage
        original_fstat, original_read, original_close = self.module.os.fstat, self.module.os.read, self.module.os.close
        original_unlink = Path.unlink

        def lstat(path, *args, **kwargs):
            if not self._is_path(path): return original_lstat(path, *args, **kwargs)
            return self._call('lstat', original_lstat, (path, *args), kwargs, is_stat=True)

        def open_file(path, *args, **kwargs):
            if not self._is_path(path): return original_open(path, *args, **kwargs)
            fd = self._call('open', original_open, (path, *args), kwargs)
            self.local.fds.add(fd)
            return fd

        def fstat(fd, *args, **kwargs):
            if not self._is_fd(fd): return original_fstat(fd, *args, **kwargs)
            return self._call('fstat', original_fstat, (fd, *args), kwargs, is_stat=True)

        def read(fd, *args, **kwargs):
            if not self._is_fd(fd): return original_read(fd, *args, **kwargs)
            return self._call('read', original_read, (fd, *args), kwargs)

        def close(fd, *args, **kwargs):
            if not self._is_fd(fd): return original_close(fd, *args, **kwargs)
            try: return self._call('close', original_close, (fd, *args), kwargs)
            finally: self.local.fds.discard(fd)

        def unlink(path, *args, **kwargs):
            if not self._is_path(path): return original_unlink(path, *args, **kwargs)
            return self._call('unlink', original_unlink, (path, *args), kwargs)

        for name, wrapper in (('lstat', lstat), ('fstat', fstat), ('read', read), ('close', close)):
            self.stack.enter_context(patch.object(self.module.os, name, wrapper))
        self.stack.enter_context(patch.object(self.module, '_open_recovered_stage', open_file))
        self.stack.enter_context(patch.object(Path, 'unlink', unlink))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

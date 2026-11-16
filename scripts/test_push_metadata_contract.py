"""Exact observed metadata-shape controls; no native Windows claim."""
import errno
import importlib.util
import os
from pathlib import Path
import stat
import types
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
FIELDS = ('st_mode','st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns','st_nlink')


def changed(value, **updates):
    fields = {key:getattr(value, key) for key in FIELDS}
    fields.update(updates)
    return types.SimpleNamespace(**fields)


class MetadataContract(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('metadata_fixture', HERE/'test_push_recovery.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.case=module.PushRecovery('runTest');self.case.setUp();self.addCleanup(self.case.tearDown)
        self.root,self.intent,name,self.payload=self.case.fixture()
        self.stage=self.case.state/'files'/name

    def rejected(self, result):
        self.assertEqual((result['status'],result['reason']),('REJECTED','OWNED_STAGED_FILE_CHANGED'))

    def test_windows_exact_cross_api_ctime_divergence_accepts_unchanged_file(self):
        lstat0,fstat0=os.lstat,os.fstat
        def lstat(path,*args,**kwargs):
            value=lstat0(path,*args,**kwargs)
            return changed(value,st_mtime_ns=1794847345259999800,st_ctime_ns=1794847345259527600) if Path(path)==self.stage else value
        def fstat(fd):
            return changed(fstat0(fd),st_mtime_ns=1794847345259999800,st_ctime_ns=1794847345259999800)
        with patch.object(self.case.mod.os,'lstat',lstat),patch.object(self.case.mod.os,'fstat',fstat):
            result=self.case.mod.recover_push(self.root)
        self.assertEqual(result['status'],'COMPLETE');self.assertFalse(self.stage.exists())

    def test_descriptor_ctime_changes_during_read_rejects_surviving_file(self):
        original=os.fstat;calls=0
        def fstat(fd):
            nonlocal calls
            calls+=1;value=original(fd)
            return changed(value,st_ctime_ns=value.st_ctime_ns+1) if calls>=2 else value
        with patch.object(self.case.mod.os,'fstat',fstat):result=self.case.mod.recover_push(self.root)
        self.rejected(result);self.assertTrue(self.stage.exists());self.assertGreaterEqual(calls,2)

    def test_final_path_ctime_change_rejects_surviving_file(self):
        original=os.lstat;calls=0
        def lstat(path,*args,**kwargs):
            nonlocal calls
            value=original(path,*args,**kwargs)
            if Path(path)==self.stage:
                calls+=1
                if calls>=2:return changed(value,st_ctime_ns=value.st_ctime_ns+1)
            return value
        with patch.object(self.case.mod.os,'lstat',lstat):result=self.case.mod.recover_push(self.root)
        self.rejected(result);self.assertTrue(self.stage.exists())

    def stale_path(self, replacement=None, probe_error=None):
        original_lstat,original_open=os.lstat,os.open
        original=original_lstat(self.stage);calls=0;opens=0
        stale=changed(original,st_ctime_ns=original.st_ctime_ns+1,st_nlink=0)
        def lstat(path,*args,**kwargs):
            nonlocal calls
            if Path(path)==self.stage:
                calls+=1
                if calls==2:
                    self.stage.unlink()
                    if replacement=='regular':self.stage.write_bytes(b'rebound')
                    elif replacement=='symlink':self.stage.symlink_to(self.case.root/'absent')
                    elif replacement=='fifo':os.mkfifo(self.stage)
                if calls>=2:
                    if calls>=3 and replacement is not None:
                        return original_lstat(path,*args,**kwargs)
                    return stale
            return original_lstat(path,*args,**kwargs)
        def open_file(path,flags,*args,**kwargs):
            nonlocal opens
            if Path(path)==self.stage:
                opens+=1
                if opens==2:
                    self.assertEqual(flags & getattr(os,'O_NONBLOCK',0),getattr(os,'O_NONBLOCK',0))
                    self.assertEqual(flags & getattr(os,'O_NOFOLLOW',0),getattr(os,'O_NOFOLLOW',0))
                    if probe_error:raise probe_error
            return original_open(path,flags,*args,**kwargs)
        with patch.object(self.case.mod.os,'lstat',lstat),patch.object(self.case.mod.os,'open',open_file):
            result=self.case.mod.recover_push(self.root)
        self.assertEqual(opens,2)
        return result

    def test_repeated_stale_nlink_zero_requires_actual_open_absence(self):
        result=self.stale_path()
        self.assertEqual(result['status'],'COMPLETE');self.assertFalse(self.stage.exists())

    def test_stale_nlink_zero_with_regular_rebound_rejects(self):
        result=self.stale_path('regular');self.rejected(result)
        self.assertEqual(self.stage.read_bytes(),b'rebound')

    @unittest.skipUnless(hasattr(os,'O_NOFOLLOW'),'no portable nofollow control on this platform')
    def test_stale_nlink_zero_with_symlink_rebound_rejects(self):
        result=self.stale_path('symlink');self.rejected(result);self.assertTrue(self.stage.is_symlink())

    def test_dangling_symlink_rebound_rejects_without_nofollow(self):
        with patch.object(self.case.mod.os,'O_NOFOLLOW',0,create=True):
            result=self.stale_path('symlink')
        self.rejected(result);self.assertTrue(self.stage.is_symlink())

    @unittest.skipUnless(hasattr(os,'mkfifo'),'FIFO control needs POSIX')
    def test_stale_nlink_zero_with_fifo_rebound_rejects_without_blocking(self):
        result=self.stale_path('fifo');self.rejected(result)
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.stage).st_mode))

    def test_absence_probe_permission_is_not_accepted(self):
        result=self.stale_path(probe_error=PermissionError(errno.EACCES,'UNIT_TEST_ONLY'))
        self.rejected(result)

    def test_descriptor_change_after_peer_unlink_requires_open_absence(self):
        original=os.fstat;calls=0
        def fstat(fd):
            nonlocal calls
            calls+=1;value=original(fd)
            if calls>=2:
                self.stage.unlink()
                return changed(value,st_ctime_ns=value.st_ctime_ns+1,st_nlink=0)
            return value
        with patch.object(self.case.mod.os,'fstat',fstat):result=self.case.mod.recover_push(self.root)
        self.assertEqual(result['status'],'COMPLETE');self.assertFalse(self.stage.exists())

    def test_initial_open_dangling_rebound_rejects_without_retry_or_nofollow(self):
        original=os.open;opens=0
        def open_file(path,flags,*args,**kwargs):
            nonlocal opens
            if Path(path)==self.stage:
                opens+=1
                self.stage.unlink();self.stage.symlink_to(self.case.root/'absent-target')
            return original(path,flags,*args,**kwargs)
        with patch.object(self.case.mod.os,'O_NOFOLLOW',0,create=True),patch.object(self.case.mod.os,'open',open_file):
            result=self.case.mod.recover_push(self.root)
        self.rejected(result);self.assertTrue(self.stage.is_symlink());self.assertEqual(opens,1)

    def test_initial_open_actual_absence_accepts_without_retry(self):
        original=os.open;opens=0
        def open_file(path,flags,*args,**kwargs):
            nonlocal opens
            if Path(path)==self.stage:opens+=1;self.stage.unlink()
            return original(path,flags,*args,**kwargs)
        with patch.object(self.case.mod.os,'open',open_file):result=self.case.mod.recover_push(self.root)
        self.assertEqual(result['status'],'COMPLETE');self.assertEqual(opens,1);self.assertFalse(self.stage.exists())

    def test_initial_open_enoent_surviving_rebound_rejects(self):
        original=os.open;opens=0
        def open_file(path,flags,*args,**kwargs):
            nonlocal opens
            if Path(path)==self.stage:
                opens+=1;self.stage.unlink();self.stage.write_bytes(b'rebound')
                raise FileNotFoundError(errno.ENOENT,'UNIT_TEST_ONLY_LOOKUP_RACE')
            return original(path,flags,*args,**kwargs)
        with patch.object(self.case.mod.os,'open',open_file):result=self.case.mod.recover_push(self.root)
        self.rejected(result);self.assertEqual(opens,1);self.assertEqual(self.stage.read_bytes(),b'rebound')


if __name__=='__main__':unittest.main(verbosity=2)

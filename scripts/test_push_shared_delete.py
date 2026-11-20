"""Windows share-delete ownership controls; native temp test runs only on Windows."""
import errno
import importlib.util
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load_fixture():
    spec = importlib.util.spec_from_file_location('shared_delete_fixture', HERE/'test_push_recovery.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.PushRecovery('runTest')


class FakeAPI:
    def __init__(self, attributes=0x20, file_type=1, failed=None):
        self.attrs,self.kind,self.failed=attributes,file_type,failed
        self.events=[]
    def create(self,*args):
        self.events.append(('create',args))
        if self.failed=='create':
            error=PermissionError(errno.EACCES,'UNIT_TEST_ONLY_CREATE_DENIED');error.winerror=5
            raise error
        return 9001
    def attributes(self,handle):
        self.events.append(('attributes',handle))
        if self.failed=='attributes':raise OSError(errno.EIO,'UNIT_TEST_ONLY_ATTRIBUTES')
        return self.attrs
    def file_type(self,handle):self.events.append(('type',handle));return self.kind
    def close(self,handle):self.events.append(('close_handle',handle))
    def to_fd(self,handle,flags):
        self.events.append(('to_fd',(handle,flags)))
        if self.failed=='convert':raise OSError(errno.EMFILE,'UNIT_TEST_ONLY_FD_CONVERSION')
        return 77


class SharedDeleteOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.case=load_fixture();self.case.setUp();self.addCleanup(self.case.tearDown)

    def open_fake(self, api):
        with patch.object(self.case.mod,'_windows_stage_api',return_value=api):
            return self.case.mod._windows_open_recovered_stage(Path('stage'),os.O_RDONLY)

    def test_share_delete_nofollow_flags_and_single_ownership_transfer(self):
        api=FakeAPI();self.assertEqual(self.open_fake(api),77)
        args=api.events[0][1]
        self.assertEqual(args,('stage',0x80000000,0x7,3,0x00200000))
        self.assertEqual([event[0] for event in api.events],['create','attributes','type','to_fd'])
        self.assertEqual(api.events[-1][1][1],os.O_RDONLY|getattr(os,'O_BINARY',0)|getattr(os,'O_NOINHERIT',0))

    def test_reparse_points_are_rejected_before_fd_conversion(self):
        for attributes in (0x400,0x410,0x420):
            with self.subTest(attributes=attributes):
                api=FakeAPI(attributes=attributes)
                with self.assertRaisesRegex(ValueError,'OWNED_STAGED_FILE_CHANGED'):self.open_fake(api)
                self.assertEqual(api.events[-1],('close_handle',9001))
                self.assertFalse(any(x[0]=='to_fd' for x in api.events))

    def test_directory_or_non_disk_handle_rejects_and_closes(self):
        for attributes,kind in ((0x10,1),(0x20,2),(0x20,3),(0x20,0)):
            with self.subTest(attributes=attributes,kind=kind):
                api=FakeAPI(attributes=attributes,file_type=kind)
                with self.assertRaises(ValueError):self.open_fake(api)
                self.assertEqual(api.events[-1],('close_handle',9001))

    def test_conversion_failure_closes_original_handle_once(self):
        api=FakeAPI(failed='convert')
        with self.assertRaises(OSError):self.open_fake(api)
        self.assertEqual([x for x in api.events if x[0]=='close_handle'],[('close_handle',9001)])

    def test_attribute_failure_closes_original_handle_once(self):
        api=FakeAPI(failed='attributes')
        with self.assertRaises(OSError):self.open_fake(api)
        self.assertEqual([x for x in api.events if x[0]=='close_handle'],[('close_handle',9001)])

    def test_create_access_denied_is_not_rewritten_as_missing(self):
        api=FakeAPI(failed='create')
        with self.assertRaises(PermissionError) as caught:self.open_fake(api)
        self.assertEqual(caught.exception.winerror,5)
        self.assertEqual([x[0] for x in api.events],['create'])

    def test_fake_native_dispatch_preserves_fstat_hash_and_terminal_contract(self):
        root,intent,name,payload=self.case.fixture()
        original_open=os.open
        class ModelAPI(FakeAPI):
            def create(inner,path,access,sharing,disposition,attributes):
                inner.events.append(('sharing',sharing));return original_open(path,os.O_RDONLY)
            def to_fd(inner,handle,flags):return handle
            def close(inner,handle):os.close(handle)
        api=ModelAPI()
        with patch.object(self.case.mod,'_windows_stage_api',return_value=api),patch.object(
                self.case.mod,'_open_recovered_stage',self.case.mod._windows_open_recovered_stage):
            result=self.case.mod.recover_push(root)
        self.assertEqual(result['status'],'COMPLETE')
        self.assertFalse((self.case.state/'files'/name).exists())
        self.assertEqual(api.events[0],('sharing',7))


@unittest.skipUnless(os.name=='nt','native share-delete test is Windows-only')
class NativeSharedDeleteTests(unittest.TestCase):
    def test_two_held_readers_allow_unlink_and_remain_readable(self):
        fixture=load_fixture();fixture.setUp()
        descriptors=[]
        temp=tempfile.TemporaryDirectory(prefix='xdev-share-delete-native-')
        observed={'event':'NATIVE_SHARED_DELETE_TEMP_ONLY','python':sys.version.split()[0],
                  'share_mask':7,'readers_held':0,'unlink':'NOT_ATTEMPTED','closed':False}
        try:
            path=Path(temp.name)/'owned-stage';payload=b'actual-native-share-delete-test'
            path.write_bytes(payload)
            for _ in range(2):descriptors.append(fixture.mod._open_recovered_stage(path,os.O_RDONLY))
            observed['readers_held']=2
            first,second=(os.fstat(fd) for fd in descriptors)
            self.assertEqual((first.st_dev,first.st_ino),(second.st_dev,second.st_ino))
            path.unlink();observed['unlink']='OK'
            try:
                additional=fixture.mod._open_recovered_stage(path,os.O_RDONLY)
            except OSError as error:
                observed['open_during_pending_delete']={'type':type(error).__name__,
                    'errno':error.errno,'winerror':getattr(error,'winerror',None)}
            else:
                os.close(additional)
                self.fail('new open unexpectedly succeeded after unlink')
            for fd in descriptors:self.assertEqual(os.read(fd,len(payload)+1),payload)
            observed['readers_after_unlink']='ORIGINAL_BYTES_VERIFIED'
        finally:
            try:
                for fd in descriptors:os.close(fd)
                observed['closed']=True
                if observed['unlink']=='OK':
                    self.assertFalse(path.exists())
                    observed['absent_after_last_close']=True
                print(json.dumps(observed,sort_keys=True),flush=True)
            finally:
                temp.cleanup()
                fixture.tearDown()


if __name__=='__main__':unittest.main(verbosity=2)

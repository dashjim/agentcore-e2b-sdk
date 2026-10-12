import importlib.util
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('check_e2b',str(Path(__file__).resolve().parents[1] / 'scripts' / 'check_e2b.py'))
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)

class CommandExitException(Exception):
    exit_code=7;stdout='partial-out';stderr='expected-error'

class FakeSandbox:
    sandbox_id='sandbox-owned-by-test'
    def __init__(self, corrupt=False, fail_kill=False):
        self.files=types.SimpleNamespace(write=self.write,read=self.read)
        self.commands=types.SimpleNamespace(run=self.run)
        self.data={};self.killed=False;self.corrupt=corrupt;self.fail_kill=fail_kill
    def write(self,path,data,**kwargs):self.data[path]=data
    def read(self,path,**kwargs):return 'corrupt' if self.corrupt else self.data[path]
    def run(self,command,**kwargs):
        if 'exit 7' in command:raise CommandExitException('sensitive-example-key')
        return types.SimpleNamespace(stdout='hello-out',stderr='hello-err',exit_code=0)
    def kill(self,**kwargs):
        self.killed=True
        if self.fail_kill:raise RuntimeError('sensitive-example-key')
        return True
    def is_running(self,**kwargs):return not self.killed

class Tests(unittest.TestCase):
    def run_fake(self,live=False,corrupt=False,fail_kill=False):
        sandbox=FakeSandbox(corrupt,fail_kill)
        api=types.SimpleNamespace(list=lambda **kw:types.SimpleNamespace(next_items=lambda:[]),create=lambda **kw:sandbox)
        e2b=types.SimpleNamespace(Sandbox=api,CommandExitException=CommandExitException)
        with patch.dict(os.environ,{'E2B_API_KEY':'sensitive-example-key'},clear=True), patch.object(mod,'version',return_value='2.51.0'),patch.dict('sys.modules',{'e2b':e2b}):
            report=mod.check_environment(live=live)
        self.assertNotIn('sensitive-example-key',json.dumps(report))
        return report,sandbox
    def test_missing_key_is_skipped_without_request(self):
        with patch.dict(os.environ,{},clear=True),patch.object(mod,'version',return_value='2.51.0'):
            result=mod.check_environment(live=True)
        self.assertEqual(result['status'],'skipped');self.assertEqual(result['checks'],[])
    def test_read_only_does_not_create(self):
        result,sandbox=self.run_fake()
        self.assertEqual(result['status'],'passed');self.assertFalse(sandbox.killed)
        self.assertEqual(len(result['checks']),1)
    def test_live_all_contracts_and_cleanup(self):
        result,sandbox=self.run_fake(live=True)
        self.assertEqual(result['status'],'passed');self.assertTrue(sandbox.killed)
        self.assertEqual(len(result['checks']),6);self.assertEqual(result['cleanup']['status'],'passed')
    def test_assertion_failure_still_cleans_up(self):
        result,sandbox=self.run_fake(live=True,corrupt=True)
        self.assertEqual(result['status'],'failed');self.assertTrue(sandbox.killed)
        self.assertEqual(result['checks'][-1]['name'],'unicode_file_roundtrip')
    def test_cleanup_failure_fails_result_without_secret(self):
        result,sandbox=self.run_fake(live=True,fail_kill=True)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['cleanup']['status'],'failed')
    def test_wrong_sdk_version_fails_without_request(self):
        with patch.dict(os.environ,{'E2B_API_KEY':'sensitive-example-key'},clear=True),patch.object(mod,'version',return_value='1.0.0'):
            result=mod.check_environment(live=True)
        self.assertEqual(result['status'],'failed');self.assertEqual(result['checks'],[])
if __name__=='__main__':unittest.main(verbosity=2)

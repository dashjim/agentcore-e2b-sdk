"""Offline lifecycle probes use injected factories and never invoke cloud APIs."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import compare_context_manager as probe

class Handle:
    def __init__(self, backend, closed=False):
        self.backend = backend
        self.closed = closed
        self.commands = self
        self.runtime = self
    @property
    def state(self):
        return 'CLOSED' if self.closed else 'READY'
    def __enter__(self):
        return self
    def __exit__(self, *args):
        if self.backend.name == 'e2b':
            self.backend.alive = False
        else:
            self.close()
    def run(self, command, timeout):
        assert timeout == 15 and not self.closed and self.backend.alive
        self.backend.calls.append(command)
        if self.backend.fail_body and command == 'printf context-ok':
            raise ValueError('SENSITIVE_EXCEPTION_TEXT')
        return SimpleNamespace(stdout=command.removeprefix('printf '))
    def export_ref(self):
        return self.backend.ref
    def is_running(self, request_timeout):
        assert request_timeout == 20
        return self.backend.alive
    def kill(self, request_timeout):
        assert request_timeout == 20
        self.backend.kills += 1
        if not self.backend.alive:
            return False
        self.backend.alive = False
        return True
    def stop(self):
        assert not self.closed, 'cleanup tried to stop via a closed handle'
        self.backend.stops += 1
        self.backend.alive = False
    def close(self):
        self.closed = True

class E2BFake(probe.E2BBackend):
    name = 'e2b'
    def __init__(self, fail_body=False):
        super().__init__()
        self.fail_body, self.alive, self.kills, self.stops = fail_body, True, 0, 0
        self.calls = []
        self.ref = 'PRIVATE_SESSION_ID'
    def create(self):
        self.sandbox = Handle(self)
        return self.sandbox

class AgentCoreFake(probe.AgentCoreBackend):
    name = 'agentcore'
    def __init__(self, fail_body=False, fail_resume=False):
        super().__init__(Path('unused'))
        self.fail_body, self.fail_resume, self.alive = fail_body, fail_resume, True
        self.kills = self.stops = self.connects = 0
        self.calls = []
        self.ref = 'PRIVATE_SESSION_ID'
        self.client = SimpleNamespace(sandboxes=SimpleNamespace(connect=self.connect), close=lambda: None)
    def create(self):
        self.sandbox = Handle(self)
        self.initial_handle = self.sandbox
        return self.sandbox
    def connect(self, ref):
        assert ref == self.ref
        self.connects += 1
        if self.fail_resume and self.connects == 1:
            raise ValueError('SENSITIVE_EXCEPTION_TEXT')
        return Handle(self)

class Tests(unittest.TestCase):
    def test_distinct_successful_lifecycles_and_redacted_report(self):
        e2b, agentcore = E2BFake(), AgentCoreFake()
        result = probe.compare_context_managers(Path('unused'), factories={'e2b': lambda: e2b, 'agentcore': lambda: agentcore})
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['summary']['lifecycle_difference_observed'])
        self.assertTrue(result['summary']['business_stdout_equal'])
        self.assertEqual(e2b.calls, ['printf context-ok'])
        self.assertEqual(agentcore.calls, ['printf context-ok', 'printf resumed'])
        self.assertEqual(e2b.kills, 1)
        self.assertEqual(agentcore.stops, 1)
        self.assertEqual(agentcore.initial_handle.state, 'CLOSED')
        self.assertIsNot(agentcore.sandbox, agentcore.initial_handle)
        self.assertNotIn('PRIVATE_SESSION_ID', json.dumps(result))
        self.assertFalse(result['backends']['e2b']['cleanup']['kill_return']['value'])
        self.assertEqual(result['backends']['agentcore']['cleanup']['status'], 'stop_request_accepted')
    def test_failed_business_still_reconnects_and_stops(self):
        backend = AgentCoreFake(fail_body=True)
        result = probe.run_backend('agentcore', backend)
        self.assertFalse(result['complete'])
        self.assertEqual(result['cleanup']['status'], 'stop_request_accepted')
        self.assertEqual(backend.stops, 1)
        self.assertEqual(backend.connects, 1)
        self.assertNotIn('SENSITIVE_EXCEPTION_TEXT', json.dumps(result))
    def test_failed_observer_reconnects_for_cleanup(self):
        backend = AgentCoreFake(fail_resume=True)
        result = probe.run_backend('agentcore', backend)
        self.assertFalse(result['complete'])
        self.assertEqual(result['observer']['status'], 'failed')
        self.assertEqual(result['cleanup']['status'], 'stop_request_accepted')
        self.assertEqual(backend.connects, 2)
        self.assertEqual(backend.stops, 1)
    def test_no_handle_is_not_successful_cleanup(self):
        backend = E2BFake()
        def fail_create():
            raise ValueError('SENSITIVE_EXCEPTION_TEXT')
        backend.create = fail_create
        result = probe.run_backend('e2b', backend)
        self.assertFalse(result['complete'])
        self.assertEqual(result['cleanup']['status'], 'no_handle')
        self.assertNotIn('SENSITIVE_EXCEPTION_TEXT', json.dumps(result))

if __name__ == '__main__':
    unittest.main()

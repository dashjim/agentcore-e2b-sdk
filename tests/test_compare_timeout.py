"""Offline timeout observations use injected factories; no cloud APIs are called."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compare_timeout as probe


class TimeoutException(Exception):
    pass


class CommandWaitTimeout(Exception):
    pass


class FakeSandbox:
    def __init__(self, name, *, unexpected_exception=False, fail_control=False, unsafe_output=False):
        self.name = name
        self.unexpected_exception = unexpected_exception
        self.fail_control = fail_control
        self.unsafe_output = unsafe_output
        self.files = self.commands = self.runtime = self
        self.calls = []
        self.data = {}
        self.stopped = self.closed = False

    def make_dir(self, path):
        return True

    def run(self, command, *, timeout, cwd=None, on_stdout=None):
        self.calls.append((command, timeout))
        if command == "pwd":
            return SimpleNamespace(stdout="/private-workspace")
        assert command == probe.COMMAND
        if timeout == 15:
            self.data[cwd] = {"started": "started", "completed": "completed"}
            return SimpleNamespace(stdout="" if self.fail_control else "startedcompleted")
        assert timeout == 0.5
        self.data[cwd] = {"started": "started"}
        if self.name == "agentcore":
            self.data[cwd]["completed"] = "completed"
        if self.unsafe_output:
            self.data[cwd]["started"] = "PRIVATE_MARKER_TEXT"
        on_stdout("PRIVATE_CALLBACK_TEXT" if self.unsafe_output else "started")
        exception = ValueError if self.unexpected_exception else TimeoutException if self.name == "e2b" else CommandWaitTimeout
        raise exception("PRIVATE_EXCEPTION_TEXT")

    def list(self, path):
        return [SimpleNamespace(name=name) for name in self.data.get(path, {})]

    def read(self, path):
        directory, name = path.rsplit("/", 1)
        return self.data[directory][name]

    def kill(self, request_timeout):
        self.stopped = True
        return True

    def is_running(self, request_timeout):
        return not self.stopped

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeE2B(probe.E2BBackend):
    def __init__(self, **kwargs):
        super().__init__()
        self.handle = FakeSandbox("e2b", **kwargs)

    def create(self):
        self.sandbox = self.handle
        return self.sandbox


class FakeAgentCore(probe.AgentCoreBackend):
    def __init__(self, **kwargs):
        super().__init__(Path("unused"))
        self.handle = FakeSandbox("agentcore", **kwargs)
        self.client = SimpleNamespace(close=lambda: None)

    def create(self):
        self.sandbox = self.handle
        return self.sandbox


class Tests(unittest.TestCase):
    def test_shared_controls_and_two_windows_preserve_observed_difference(self):
        e2b, agentcore = FakeE2B(), FakeAgentCore()
        with patch.object(probe.time, "sleep") as sleep:
            result = probe.compare_timeouts(Path("unused"), factories={"e2b": lambda: e2b, "agentcore": lambda: agentcore})
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3.2, 3.0, 3.2, 3.0])
        self.assertEqual(e2b.handle.calls, agentcore.handle.calls)
        for name, backend in (("e2b", e2b), ("agentcore", agentcore)):
            observation = result["backends"][name]
            self.assertTrue(observation["probe"]["control"]["passed"])
            self.assertTrue(observation["expected_timeout_observed"])
            self.assertTrue(backend.handle.stopped)
            for window in observation["probe"]["windows"]:
                self.assertTrue(window["markers"]["started"]["present"])
                self.assertEqual(window["markers"]["completed"]["present"], name == "agentcore")
        self.assertNotIn("PRIVATE_EXCEPTION_TEXT", json.dumps(result))
        self.assertIn("cannot establish permanent cancellation", result["interpretation_limit"])

    def test_unexpected_exception_fails_but_observes_both_windows_and_cleans_up(self):
        backend = FakeE2B(unexpected_exception=True)
        with patch.object(probe.time, "sleep"):
            result = probe.run_backend("e2b", backend)
        self.assertFalse(result["complete"])
        self.assertFalse(result["expected_timeout_observed"])
        self.assertEqual(len(result["probe"]["windows"]), 2)
        self.assertEqual(result["cleanup"]["status"], "passed")
        self.assertTrue(backend.handle.stopped)

    def test_failed_control_prevents_short_probe_and_still_stops_agentcore(self):
        backend = FakeAgentCore(fail_control=True)
        with patch.object(probe.time, "sleep") as sleep:
            result = probe.run_backend("agentcore", backend)
        self.assertFalse(result["complete"])
        self.assertFalse(result["probe"]["control"]["passed"])
        self.assertEqual(result["probe"]["windows"], [])
        self.assertEqual(result["cleanup"]["status"], "stop_request_accepted")
        self.assertTrue(backend.handle.stopped)
        self.assertTrue(backend.handle.closed)
        sleep.assert_not_called()

    def test_arbitrary_callback_marker_and_exception_text_are_omitted(self):
        backend = FakeE2B(unsafe_output=True)
        with patch.object(probe.time, "sleep"):
            result = probe.run_backend("e2b", backend)
        serialized = json.dumps(result)
        for forbidden in ("PRIVATE_CALLBACK_TEXT", "PRIVATE_MARKER_TEXT", "PRIVATE_EXCEPTION_TEXT", "/private-workspace"):
            self.assertNotIn(forbidden, serialized)
        callback = result["probe"]["short_timeout"]["callbacks_at_return"][0]
        self.assertEqual(callback["text"]["omitted"], "unexpected_probe_output")
        self.assertEqual(result["probe"]["windows"][0]["markers"]["started"]["content"]["omitted"], "unexpected_probe_output")


if __name__ == "__main__":
    unittest.main()

"""Local regression checks for setup ownership and JSON result comparisons."""
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "lib/rr_job.py"
COMPARE = ROOT / "tests/compare_json.py"


def is_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.025)
    return predicate()


class SetupOwnership(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="remote-run-setup-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.job = self.root / "jobs/fixture"
        (self.job / "src").mkdir(parents=True)
        self.pidfile = self.root / "owned-setup.pid"
        self.network_used = self.root / "network-command-used"
        guard = self.root / "guard"
        guard.mkdir()
        for command in ["ssh", "rsync", "tmux", "tailscale"]:
            path = guard / command
            path.write_text("#!/bin/sh\n: > " + shlex.quote(str(self.network_used)) + "\nexit 99\n")
            path.chmod(0o755)
        caffeinate = guard / "caffeinate"
        caffeinate.write_text("#!/bin/sh\nexit 0\n")
        caffeinate.chmod(0o755)
        self.env = dict(os.environ, PATH=str(guard) + ":" + os.environ.get("PATH", ""))
        self.payload = {
            "job_id": "fixture", "remote_root": str(self.root), "project": "fixture",
            "command": "/usr/bin/true", "workdir": "", "env": {}, "grace_seconds": 0,
            "setup": "exec " + shlex.join([
                sys.executable, "-c",
                "import os,time; from pathlib import Path; Path("
                + repr(str(self.pidfile)) + ").write_text(str(os.getpid())); time.sleep(60)",
            ]),
        }
        (self.job / "job.json").write_text(json.dumps(self.payload))
        self.wrapper = subprocess.Popen(
            [sys.executable, str(HELPER), "run", str(self.job)], env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.child_pid = None
        self.addCleanup(self.stop_owned_processes)
        self.assertTrue(wait_until(self.pidfile.exists, 5), "setup did not start")
        self.child_pid = int(self.pidfile.read_text())
        self.wrapper.kill()
        self.wrapper.wait(timeout=3)
        self.assertTrue(is_alive(self.child_pid), "setup did not survive its wrapper")

    def stop_owned_processes(self):
        if self.wrapper.poll() is None:
            self.wrapper.kill()
            self.wrapper.wait(timeout=3)
        if self.child_pid is not None:
            try:
                os.killpg(self.child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if not wait_until(lambda: not is_alive(self.child_pid)):
                try:
                    os.killpg(self.child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.assertFalse(self.network_used.exists(), "a network command was invoked")

    def helper(self, *args):
        return subprocess.run(
            [sys.executable, str(HELPER), *map(str, args)], env=self.env,
            capture_output=True, text=True, timeout=20,
        )

    def test_setup_keeps_host_lock_after_wrapper_death(self):
        with (self.root / "host.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                available = False
            else:
                available = True
                fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertFalse(available, "setup is alive but another job can acquire the host lock")
        contender = self.root / "jobs/contender"
        (contender / "src").mkdir(parents=True)
        (contender / "job.json").write_text(json.dumps(dict(self.payload, job_id="contender")))
        result = self.helper("run", contender)
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse((contender / "run.log").exists())

    def test_cancel_stops_setup_after_wrapper_death(self):
        status = self.helper("status", self.job)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["state"], "lost")
        result = self.helper("cancel", self.job)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "cancelled")
        self.assertTrue(
            wait_until(lambda: not is_alive(self.child_pid)),
            "cancel reported success but the owned setup process is still alive",
        )
        with (self.root / "host.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


class ComparisonResults(unittest.TestCase):
    def test_boolean_count_and_nonfinite_contracts(self):
        cases = [
            ("equal counts", {"count": 2}, {"count": 2}, 0),
            ("different counts", {"count": 1}, {"count": 2}, 1),
            ("boolean and count", {"count": True}, {"count": 1}, 1),
            ("zero and boolean", {"count": 0}, {"count": False}, 1),
            ("boolean and float", {"value": True}, {"value": 1.0}, 1),
            ("finite tolerance", {"value": 1.0}, {"value": 1.00000000001}, 0),
            ("outside tolerance", {"value": 1.0}, {"value": 1.1}, 1),
            ("exact mixed count", {"count": 2}, {"count": 2.0}, 0),
            ("mixed count differs", {"count": 1}, {"count": 1.00000000001}, 1),
            ("large count differs", {"count": 9007199254740993}, {"count": 9007199254740992.0}, 1),
            ("large count matches", {"count": 9007199254740992}, {"count": 9007199254740992.0}, 0),
            ("NaN and finite", {"value": float("nan")}, {"value": 1.0}, 1),
            ("finite and NaN", {"value": 1.0}, {"value": float("nan")}, 1),
            ("infinity and finite", {"value": float("inf")}, {"value": 1.0}, 1),
            ("finite and infinity", {"value": 1.0}, {"value": float("-inf")}, 1),
            ("opposite infinities", {"value": float("inf")}, {"value": float("-inf")}, 1),
            ("matching infinities", {"value": float("inf")}, {"value": float("inf")}, 0),
            ("matching NaNs", {"value": float("nan")}, {"value": float("nan")}, 0),
        ]
        with tempfile.TemporaryDirectory(prefix="remote-run-compare-") as directory:
            left, right = Path(directory) / "left.json", Path(directory) / "right.json"
            for name, local, remote, expected in cases:
                with self.subTest(name=name):
                    left.write_text(json.dumps(local))
                    right.write_text(json.dumps(remote))
                    result = subprocess.run(
                        [sys.executable, str(COMPARE), str(left), str(right)],
                        capture_output=True, text=True, timeout=3,
                    )
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    self.assertIn("mismatches:", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)

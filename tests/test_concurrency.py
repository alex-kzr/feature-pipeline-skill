"""Contention, stale-owner, and Cargo-serialization tests for the write leases and mutex.

Multiprocess by construction where the requirement is cross-process: child writers are real
``python -c`` processes so a passing test proves the lock file — not a thread lock — is what
serializes them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline_core.concurrency import (
    FileMutex,
    PipelineLease,
    cargo_lock_path,
    cargo_mutex,
    pipeline_lease,
    task_lease,
    task_lock_path,
)
from pipeline_core.lease import LeaseHeldError
from pipeline_core.state import read_lease, write_lease

SKILL_ROOT = Path(__file__).resolve().parents[1]


def _child(code: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(SKILL_ROOT))
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def _dead_pid() -> int:
    """A PID that has certainly exited."""
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait(timeout=30)
    return done.pid


class WriteLeaseTests(unittest.TestCase):
    def test_live_pipeline_lease_excludes_another_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            held = pipeline_lease(root, "run-a")
            held.acquire("T-1")
            try:
                result = _child(
                    "import sys\n"
                    "from pipeline_core.concurrency import pipeline_lease\n"
                    "from pipeline_core.lease import LeaseHeldError\n"
                    "try:\n"
                    "    pipeline_lease(sys.argv[1], 'run-b').acquire()\n"
                    "    print('ACQUIRED')\n"
                    "except LeaseHeldError:\n"
                    "    print('HELD')\n",
                    str(root),
                )
                self.assertIn("HELD", result.stdout)
                self.assertNotIn("ACQUIRED", result.stdout)
            finally:
                held.release()

    def test_task_leases_are_independent_but_exclusive_per_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = task_lease(root, "run", "T-1")
            second = task_lease(root, "run", "T-2")
            first.acquire("T-1")
            second.acquire("T-2")
            with self.assertRaises(LeaseHeldError):
                task_lease(root, "run", "T-1").acquire()
            first.release()
            second.release()
            self.assertFalse(task_lock_path(root, "T-1").exists())
            self.assertFalse(task_lock_path(root, "T-2").exists())

    def test_dead_owner_is_reclaimed_but_unreadable_lease_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.lock"

            write_lease(path, "old-run", _dead_pid(), "T-1")
            PipelineLease(path, "new-run").acquire("T-1")
            self.assertEqual(read_lease(path)["run_id"], "new-run")

            path.write_text("{ not valid json", encoding="utf-8")
            with self.assertRaises(LeaseHeldError):
                PipelineLease(path, "later-run").acquire()

    def test_release_only_removes_a_lease_owned_by_this_run_and_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.lock"
            owner = PipelineLease(path, "run-a", pid=os.getpid())
            owner.acquire("T-1")

            PipelineLease(path, "run-b", pid=os.getpid() + 1).release()
            self.assertTrue(path.exists())
            self.assertEqual(read_lease(path)["run_id"], "run-a")

            owner.release()
            self.assertFalse(path.exists())

    def test_exception_inside_the_context_manager_still_releases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.lock"
            with self.assertRaises(RuntimeError):
                with PipelineLease(path, "run-a"):
                    raise RuntimeError("boom")
            self.assertFalse(path.exists())


class CargoMutexTests(unittest.TestCase):
    def test_file_mutex_serializes_two_independent_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "mutex.log"
            path = root / "cargo.lock"
            code = (
                "import os, sys, time\n"
                "from pipeline_core.concurrency import FileMutex\n"
                "with FileMutex(sys.argv[1], timeout=20):\n"
                "    open(sys.argv[2], 'a').write(f'enter {os.getpid()} {time.time()}\\n')\n"
                "    time.sleep(0.5)\n"
                "    open(sys.argv[2], 'a').write(f'exit {os.getpid()} {time.time()}\\n')\n"
            )
            workers = [
                subprocess.Popen(
                    [sys.executable, "-c", code, str(path), str(log)],
                    env=dict(os.environ, PYTHONPATH=str(SKILL_ROOT)))
                for _ in range(2)
            ]
            for worker in workers:
                worker.wait(timeout=30)

            self.assertFalse(path.exists())
            self._assert_no_overlap(log)

    def test_cargo_command_takes_the_mutex_so_runs_never_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prompt.md").write_text("prompt", encoding="utf-8")
            log = root / "cargo-runs.log"
            cargo = self._make_fake_cargo(root, log)

            code = (
                "import sys\n"
                "from pathlib import Path\n"
                "from pipeline_core.state import Run\n"
                "from pipeline_core.commands import run_command\n"
                "root = Path(sys.argv[1])\n"
                "run = Run.create('c', root / 'prompt.md', None, root / 'runs' / sys.argv[3], root)\n"
                "rec = run_command(run, 'stage:build', '.', [sys.argv[2], str(root / 'cargo-runs.log')])\n"
                "print(rec['exit_code'])\n"
            )
            env = dict(os.environ, PYTHONPATH=str(SKILL_ROOT))
            workers = [
                subprocess.Popen(
                    [sys.executable, "-c", code, str(root), str(cargo), f"run-{n}"],
                    stdout=subprocess.PIPE, text=True, env=env)
                for n in range(2)
            ]
            exits = [worker.communicate(timeout=40)[0].strip() for worker in workers]

            self.assertEqual(exits, ["0", "0"])
            self.assertFalse(cargo_lock_path(root).exists())
            self._assert_no_overlap(log)

    def test_mutex_reclaims_a_dead_owner_and_times_out_on_a_live_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = cargo_lock_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"pid": _dead_pid(), "run_id": "old"}), encoding="utf-8")
            with cargo_mutex(root, run_id="new", timeout=5):
                self.assertEqual(read_lease(path)["run_id"], "new")
            self.assertFalse(path.exists())

            live = FileMutex(path, run_id="holder")
            live.acquire()
            try:
                result = _child(
                    "import sys\n"
                    "from pipeline_core.concurrency import FileMutex, MutexTimeout\n"
                    "try:\n"
                    "    FileMutex(sys.argv[1], timeout=0.4).acquire()\n"
                    "    print('ACQUIRED')\n"
                    "except MutexTimeout:\n"
                    "    print('TIMEOUT')\n",
                    str(path),
                )
                self.assertIn("TIMEOUT", result.stdout)
            finally:
                live.release()

    # -- helpers ----------------------------------------------------------------------

    def _make_fake_cargo(self, directory: Path, log: Path) -> Path:
        worker = directory / "cargo_worker.py"
        worker.write_text(
            "import os, sys, time\n"
            "log = sys.argv[1]\n"
            "open(log, 'a').write(f'enter {os.getpid()} {time.time()}\\n')\n"
            "time.sleep(0.5)\n"
            "open(log, 'a').write(f'exit {os.getpid()} {time.time()}\\n')\n",
            encoding="utf-8")
        if os.name == "nt":
            cargo = directory / "cargo.bat"
            cargo.write_text(
                f'@echo off\r\n"{sys.executable}" "{worker}" %*\r\n', encoding="utf-8")
        else:
            cargo = directory / "cargo"
            cargo.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{worker}" "$@"\n', encoding="utf-8")
            cargo.chmod(0o755)
        return cargo

    def _assert_no_overlap(self, log: Path) -> None:
        intervals: dict[str, dict[str, float]] = {}
        for line in log.read_text(encoding="utf-8").splitlines():
            event, pid, stamp = line.split()
            intervals.setdefault(pid, {})[event] = float(stamp)
        spans = sorted((v["enter"], v["exit"]) for v in intervals.values())
        self.assertEqual(len(spans), 2, f"expected two cargo runs, got {intervals}")
        (a_enter, a_exit), (b_enter, b_exit) = spans
        self.assertLessEqual(a_exit, b_enter, f"cargo runs overlapped: {spans}")


if __name__ == "__main__":
    unittest.main()

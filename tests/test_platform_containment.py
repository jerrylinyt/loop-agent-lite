"""Focused process-containment capability and Windows Job-policy tests."""

from __future__ import annotations

import subprocess
import sys
import unittest

from engine import platform_compat as compat


@unittest.skipUnless(compat.IS_WINDOWS, "Windows Job Object policy")
class WindowsContainmentCapabilityTest(unittest.TestCase):
    @staticmethod
    def _wait_closed(process: subprocess.Popen) -> None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def test_guardian_looking_argv_cannot_bypass_job_assignment(self):
        # Flush any abandoned one-shot from an earlier failed test spawn.
        compat.popen_group_kwargs()
        compat.popen_group_kwargs()
        process = subprocess.Popen(
            [
                sys.executable, "-c", "import time; time.sleep(30)",
                "-m", "engine.parallel_child", "--run-dir", "spoofed",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **compat.popen_group_kwargs(),
        )
        try:
            self.assertIs(compat.attach_process_group(process), True)
            self.assertNotEqual(
                getattr(process, "_loop_group_kind", None),
                "guardian-control-pipe",
            )
            self.assertTrue(getattr(process, "_loop_job_handle", None))
            self.assertTrue(compat.verify_process_group_containment(process))
        finally:
            compat.close_process_group(process)
            self._wait_closed(process)

    def test_trusted_one_shot_capability_not_argv_grants_guardian_lease(self):
        compat.request_process_group_breakaway()
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **compat.popen_group_kwargs(),
        )
        try:
            self.assertIs(compat.attach_process_group(process), True)
            self.assertEqual(
                getattr(process, "_loop_group_kind", None),
                "guardian-control-pipe",
            )
            self.assertIsNone(getattr(process, "_loop_job_handle", None))
        finally:
            process.kill()
            self._wait_closed(process)

    def test_abandoned_capability_does_not_leak_past_next_group_kwargs(self):
        compat.request_process_group_breakaway()
        compat.popen_group_kwargs()  # model Popen failing before attach
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **compat.popen_group_kwargs(),
        )
        try:
            self.assertIs(compat.attach_process_group(process), True)
            self.assertNotEqual(
                getattr(process, "_loop_group_kind", None),
                "guardian-control-pipe",
            )
        finally:
            compat.close_process_group(process)
            self._wait_closed(process)


if __name__ == "__main__":
    unittest.main()

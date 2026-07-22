"""Managed gate client publishes, waits, and cancels without Git authority."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from engine import parallel_gate
from engine import loop as loop_mod
from engine import parallel
from engine import platform_compat as compat
from engine.parallel_spool import DurableSpool


RUN_ID = "a1b2c3d4"
REQUEST_ID = "b" * 32
SHA = "a" * 40
REPO_ROOT = Path(__file__).resolve().parent.parent


def gate_env() -> dict[str, str]:
    return {
        "RUN_ID": RUN_ID,
        "TASK": "2",
        "REQUEST_ID": REQUEST_ID,
        "VALIDATED_SHA": SHA,
        "VALIDATED_ROUND": "7",
        "RUN_CONFIG_HASH": "1" * 64,
        "LAUNCH_SPEC_HASH": "2" * 64,
        "MANIFEST_HASH": "3" * 64,
    }


def response(status="merged", returncode=0, reason=None):
    payload = {
        "status": status,
        "run_id": RUN_ID,
        "task": 2,
        "request_id": REQUEST_ID,
        "validated_sha": SHA,
    }
    if reason is not None:
        payload["reason"] = reason
    return {
        "schema": 1, "request_id": REQUEST_ID,
        "returncode": returncode, "response": payload,
    }


class TestParallelGateClient(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name) / "run"
        self.run_dir.mkdir()
        self.spool = DurableSpool(
            self.run_dir / "requests", responses_root=self.run_dir / "responses")

    def test_claimed_request_returns_exact_supervisor_response(self):
        failures = []

        def supervisor():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                record = self.spool.get_request(REQUEST_ID)
                if record is not None:
                    try:
                        claimed = self.spool.claim_request(REQUEST_ID)
                        self.assertTrue(claimed.transitioned)
                        self.spool.publish_response(REQUEST_ID, response())
                    except BaseException as exc:  # assertion is relayed to main test thread
                        failures.append(exc)
                    return
                time.sleep(0.005)
            failures.append(AssertionError("request was never published"))

        thread = threading.Thread(target=supervisor)
        thread.start()
        rc, payload = parallel_gate.execute_gate(
            self.run_dir, env=gate_env(), wait_timeout=2, poll_interval=0.005)
        thread.join(timeout=2)
        if failures:
            raise failures[0]
        self.assertEqual(rc, 0)
        self.assertEqual(payload, response()["response"])
        self.assertEqual(self.spool.get_request(REQUEST_ID).state, "claimed")

    def test_deadline_cancels_only_pending_request(self):
        rc, payload = parallel_gate.execute_gate(
            self.run_dir, env=gate_env(), wait_timeout=0.01, poll_interval=0.001)
        self.assertEqual(rc, 11)
        self.assertEqual(payload["status"], "supervisor-lost-before-claim")
        self.assertEqual(self.spool.get_request(REQUEST_ID).state, "cancelled")

    def test_deadline_after_claim_is_recovery_required_and_not_cancelled(self):
        failures = []

        def claim_only():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self.spool.get_request(REQUEST_ID) is not None:
                    try:
                        self.spool.claim_request(REQUEST_ID)
                    except BaseException as exc:
                        failures.append(exc)
                    return
                time.sleep(0.001)

        thread = threading.Thread(target=claim_only)
        thread.start()
        rc, payload = parallel_gate.execute_gate(
            self.run_dir, env=gate_env(), wait_timeout=0.05, poll_interval=0.001)
        thread.join(timeout=2)
        if failures:
            raise failures[0]
        self.assertEqual(rc, 31)
        self.assertEqual(payload["status"], "recovery-required-after-claim")
        self.assertEqual(self.spool.get_request(REQUEST_ID).state, "claimed")

    def test_external_cancel_does_not_look_like_client_won_safe_retry(self):
        failures = []

        def cancel_as_supervisor():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self.spool.get_request(REQUEST_ID) is not None:
                    try:
                        result = self.spool.cancel_request(REQUEST_ID)
                        self.assertTrue(result.transitioned)
                    except BaseException as exc:
                        failures.append(exc)
                    return
                time.sleep(0.001)

        thread = threading.Thread(target=cancel_as_supervisor)
        thread.start()
        rc, payload = parallel_gate.execute_gate(
            self.run_dir, env=gate_env(), wait_timeout=0.05, poll_interval=0.001)
        thread.join(timeout=2)
        if failures:
            raise failures[0]
        self.assertEqual(rc, 31)
        self.assertEqual(payload["status"], "recovery-required-after-claim")
        self.assertEqual(self.spool.get_request(REQUEST_ID).state, "cancelled")

    def test_response_is_strictly_bound_to_request(self):
        request = parallel_gate.request_from_environment(
            gate_env(), deadline_at="2026-01-01T00:00:00+00:00")
        self.spool.publish_request(REQUEST_ID, request)
        self.spool.claim_request(REQUEST_ID)
        bad = response()
        bad["response"]["validated_sha"] = "c" * 40
        record = self.spool.publish_response(REQUEST_ID, bad).record
        with self.assertRaisesRegex(parallel_gate.GateClientError, "不符合 request"):
            parallel_gate._validate_durable_response(record, request)

    def test_success_response_cannot_precede_request_claim(self):
        request = parallel_gate.request_from_environment(
            gate_env(), deadline_at="2026-01-01T00:00:00+00:00")
        self.spool.publish_request(REQUEST_ID, request)
        record = self.spool.publish_response(REQUEST_ID, response()).record

        with self.assertRaisesRegex(parallel_gate.GateClientError, "claimed"):
            parallel_gate._validate_response_linearization(
                self.spool, request, record)

    def test_pause_response_requires_and_accepts_cancelled_request(self):
        request = parallel_gate.request_from_environment(
            gate_env(), deadline_at="2026-01-01T00:00:00+00:00")
        self.spool.publish_request(REQUEST_ID, request)
        paused = response(status="paused", returncode=20,
                          reason="parent supervisor received Pause")
        pending_response = self.spool.publish_response(
            REQUEST_ID, paused).record
        with self.assertRaisesRegex(parallel_gate.GateClientError, "cancelled"):
            parallel_gate._validate_response_linearization(
                self.spool, request, pending_response)

        self.spool.cancel_request(REQUEST_ID)
        rc, payload = parallel_gate._validate_response_linearization(
            self.spool, request, pending_response)
        self.assertEqual(rc, 20)
        self.assertEqual(payload["status"], "paused")

    def test_request_carries_both_config_hashes_and_deadline(self):
        request = parallel_gate.request_from_environment(
            gate_env(), deadline_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(set(request), {
            "schema", "run_id", "task", "request_id", "validated_sha",
            "validated_round", "run_config_hash", "launch_spec_hash",
            "manifest_hash", "deadline_at",
        })
        self.assertEqual(request["validated_round"], 7)
        self.assertEqual(request["launch_spec_hash"], "2" * 64)

    def test_supervisor_response_builder_rejects_status_rc_mismatch(self):
        request = parallel_gate.request_from_environment(
            gate_env(), deadline_at="2026-01-01T00:00:00+00:00")
        envelope = parallel_gate.durable_response_envelope(
            request, returncode=10, status="stale-integration")
        self.assertEqual(envelope["request_id"], REQUEST_ID)
        self.assertEqual(envelope["response"]["status"], "stale-integration")
        with self.assertRaisesRegex(parallel_gate.GateClientError, "不合法"):
            parallel_gate.durable_response_envelope(
                request, returncode=0, status="stale-integration")
        with self.assertRaisesRegex(
                parallel_gate.GateClientError, "nonterminal"):
            parallel_gate.durable_response_envelope(
                request, returncode=31,
                status="recovery-required-after-claim")

    def test_real_client_inner_deadline_precedes_worker_watchdog(self):
        inner_timeout = 0.05
        gate_cmd = compat.split_command(parallel.build_gate_client_command(
            python_executable=sys.executable,
            run_dir=self.run_dir,
            wait_timeout=inner_timeout,
        ))
        env = loop_mod.expose_project_package({**os.environ, **gate_env()})

        rc, stdout, stderr, timed_out = loop_mod.run_completion_gate(
            gate_cmd, REPO_ROOT, env,
            inner_timeout + loop_mod.GATE_CLIENT_GRACE_SEC,
        )

        self.assertFalse(timed_out, stderr)
        self.assertEqual(rc, 11, stderr)
        self.assertEqual(
            json.loads(stdout)["status"], "supervisor-lost-before-claim")


if __name__ == "__main__":
    unittest.main()

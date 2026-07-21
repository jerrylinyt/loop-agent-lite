"""Managed-worker boundary contracts are strict and Git-side-effect free."""

import json
import unittest

from engine import parallel_contract as contract


RUN_ID = "a1b2c3d4"
SHA = "a" * 40
REQUEST_ID = "request-1"


def response(status: str, **updates) -> str:
    payload = {
        "status": status,
        "run_id": RUN_ID,
        "task": 3,
        "request_id": REQUEST_ID,
        "validated_sha": SHA,
    }
    payload.update(updates)
    return json.dumps(payload)


class TestSafeIntegrationRef(unittest.TestCase):
    def test_round_trip_uses_canonical_full_ref(self):
        ref = contract.integration_ref_for(RUN_ID)
        self.assertEqual(ref, "refs/heads/loop/a1b2c3d4/integration")
        self.assertEqual(contract.run_id_from_integration_ref(ref), RUN_ID)

    def test_rejects_short_names_metacharacters_and_wrong_length(self):
        for value in (
            "loop/a1b2c3d4/integration",
            "refs/heads/loop/A1B2C3D4/integration",
            "refs/heads/loop/a1b2c3d/integration",
            "refs/heads/loop/a1b2c3d4/integration;touch-pwned",
        ):
            with self.subTest(value=value), self.assertRaises(contract.ParallelContractError):
                contract.run_id_from_integration_ref(value)

    def test_managed_sync_text_only_contains_validated_ref_and_terminal_block(self):
        ref = contract.integration_ref_for(RUN_ID)
        text = contract.managed_sync_instructions(
            ref, "/runtime/python -m engine.work block --reason"
        )
        self.assertIn(f"git merge --no-edit {ref}", text)
        self.assertIn("未知 merge-in-progress", text)
        self.assertIn("block --reason", text)
        self.assertIn("不得 done", text)


class TestGateResponse(unittest.TestCase):
    def parse(self, returncode: int, output: str):
        return contract.parse_gate_response(
            returncode,
            output,
            run_id=RUN_ID,
            task=3,
            request_id=REQUEST_ID,
            validated_sha=SHA,
        )

    def test_accepts_each_documented_rc_status_pair(self):
        pairs = (
            (0, "merged"),
            (0, "already-merged"),
            (10, "stale-integration"),
            (11, "busy"),
            (11, "supervisor-lost-before-claim"),
            (20, "paused"),
            (21, "cancelled"),
            (30, "fatal-invariant"),
            (31, "recovery-required-after-claim"),
        )
        for returncode, status in pairs:
            with self.subTest(returncode=returncode, status=status):
                result = self.parse(returncode, response(status))
                self.assertEqual(result.status, status)

    def test_rejects_malformed_multi_line_unknown_and_mismatched_responses(self):
        cases = (
            (0, "not-json"),
            (0, response("merged") + "\n" + response("merged")),
            (99, response("merged")),
            (10, response("merged")),
            (0, response("merged", task=4)),
            (0, response("merged", surprise=True)),
            (0, response("merged", reason="")),
        )
        for returncode, output in cases:
            with self.subTest(returncode=returncode, output=output), \
                    self.assertRaises(contract.ParallelContractError):
                self.parse(returncode, output)


if __name__ == "__main__":
    unittest.main()

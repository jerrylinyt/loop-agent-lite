"""Durable spool publication, CAS transitions, and recovery invariants."""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import parallel_spool as spool_mod


REQUEST_A = "a" * 32
REQUEST_B = "b" * 32
REQUEST_C = "c" * 32
REQUEST_D = "d" * 32


def request_payload(request_id: str, **updates) -> dict:
    payload = {
        "schema_version": 1,
        "request_id": request_id,
        "run_id": "a1b2c3d4",
        "task": 2,
    }
    payload.update(updates)
    return payload


def _transition_worker(requests_root, responses_root, action, start_event, output):
    """Spawn-safe claim/cancel contender used by the cross-process CAS test."""
    try:
        spool = spool_mod.DurableSpool(
            Path(requests_root), responses_root=Path(responses_root))
        start_event.wait(15)
        result = (spool.claim_request(REQUEST_A) if action == "claim"
                  else spool.cancel_request(REQUEST_A))
        output.put((action, result.transitioned, result.state, None))
    except BaseException as exc:  # noqa: BLE001 - child failure must reach parent assertion
        output.put((action, False, None, f"{type(exc).__name__}:{exc}"))


class TestDurableSpool(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.requests = self.root / "requests"
        self.responses = self.root / "responses"
        self.spool = spool_mod.DurableSpool(
            self.requests, responses_root=self.responses)

    def publish(self, request_id=REQUEST_A, **updates):
        return self.spool.publish_request(
            request_id, request_payload(request_id, **updates))

    def test_publish_uses_complete_private_stage_then_exposes_pending(self):
        record = self.publish(deadline="2030-01-01T00:00:00Z")

        self.assertEqual(record.state, "pending")
        self.assertEqual(record.path, self.requests / "pending" / f"{REQUEST_A}.json")
        self.assertEqual(record.raw, record.path.read_bytes())
        self.assertTrue(record.raw.endswith(b"\n"))
        self.assertEqual(self.spool.get_request(REQUEST_A).payload["task"], 2)
        self.assertEqual([item.request_id for item in self.spool.list_requests()], [REQUEST_A])
        self.assertEqual(list((self.requests / "staging").iterdir()), [])

    def test_publish_fsyncs_complete_stage_before_atomic_replace(self):
        events = []
        real_fsync = spool_mod.os.fsync
        real_replace = spool_mod._replace_with_retry

        def observed_fsync(fd):
            events.append(("fsync", fd))
            return real_fsync(fd)

        def observed_replace(source, target):
            events.append(("replace", source.read_bytes(), target.exists()))
            return real_replace(source, target)

        with mock.patch.object(spool_mod.os, "fsync", side_effect=observed_fsync), \
                mock.patch.object(
                    spool_mod, "_replace_with_retry", side_effect=observed_replace):
            record = self.publish()

        replace_index = next(
            index for index, event in enumerate(events) if event[0] == "replace")
        self.assertTrue(any(event[0] == "fsync" for event in events[:replace_index]))
        replace_event = events[replace_index]
        self.assertEqual(replace_event[1], record.raw)
        self.assertFalse(replace_event[2], "final path must be absent before publish")

    def test_partial_and_complete_staging_are_never_consumer_visible(self):
        partial_dir = self.requests / "staging" / ("1" * 32)
        partial_dir.mkdir()
        (partial_dir / "request.json").write_bytes(b'{"request_id":')
        complete_dir = self.requests / "staging" / ("2" * 32)
        complete_dir.mkdir()
        (complete_dir / "request.json").write_text(
            json.dumps(request_payload(REQUEST_B)), encoding="utf-8")

        self.assertEqual(self.spool.list_requests(), ())
        snapshot = self.spool.scan_recovery()
        by_id = {artifact.staging_id: artifact for artifact in snapshot.staging}
        self.assertFalse(by_id["1" * 32].complete)
        self.assertIsNotNone(by_id["1" * 32].error)
        self.assertTrue(by_id["2" * 32].complete)
        self.assertEqual(by_id["2" * 32].request_id, REQUEST_B)

    def test_duplicate_request_id_is_rejected_in_every_durable_state(self):
        transitions = {
            "pending": lambda spool: None,
            "claimed": lambda spool: spool.claim_request(REQUEST_A),
            "cancelled": lambda spool: spool.cancel_request(REQUEST_A),
            "response": lambda spool: (
                spool.claim_request(REQUEST_A),
                spool.publish_response(
                    REQUEST_A, request_payload(REQUEST_A, status="merged")),
            ),
        }
        for state, transition in transitions.items():
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                spool = spool_mod.DurableSpool(root / "requests")
                spool.publish_request(REQUEST_A, request_payload(REQUEST_A))
                transition(spool)
                with self.assertRaises(spool_mod.DuplicateRequestError):
                    spool.publish_request(REQUEST_A, request_payload(REQUEST_A))

        # Even an orphan response remains a durable reservation of the id; it
        # must never permit request-id reuse while recovery diagnoses corruption.
        claimed = self.spool.claim_request(REQUEST_A) if self.spool.get_request(REQUEST_A) else None
        if claimed is None:
            self.publish()
            self.spool.claim_request(REQUEST_A)
        self.spool.publish_response(
            REQUEST_A, request_payload(REQUEST_A, status="merged"))
        (self.requests / "claimed" / f"{REQUEST_A}.json").unlink()
        with self.assertRaises(spool_mod.DuplicateRequestError):
            self.publish()

    def test_claim_and_cancel_are_idempotent_loser_observations(self):
        self.publish(REQUEST_A)
        claimed = self.spool.claim_request(REQUEST_A)
        lost_cancel = self.spool.cancel_request(REQUEST_A)
        replay_claim = self.spool.claim_request(REQUEST_A)
        self.assertTrue(claimed.transitioned)
        self.assertEqual(claimed.state, "claimed")
        self.assertFalse(lost_cancel.transitioned)
        self.assertEqual(lost_cancel.state, "claimed")
        self.assertFalse(replay_claim.transitioned)

        self.publish(REQUEST_B)
        cancelled = self.spool.cancel_request(REQUEST_B)
        lost_claim = self.spool.claim_request(REQUEST_B)
        self.assertTrue(cancelled.transitioned)
        self.assertEqual(cancelled.state, "cancelled")
        self.assertFalse(lost_claim.transitioned)
        self.assertEqual(lost_claim.state, "cancelled")

    def test_claim_vs_cancel_cross_process_has_exactly_one_winner(self):
        self.publish()
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        output = context.Queue()
        processes = [
            context.Process(
                target=_transition_worker,
                args=(str(self.requests), str(self.responses), action, start_event, output),
            )
            for action in ("claim", "cancel")
        ]
        for process in processes:
            process.start()
        start_event.set()
        results = [output.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                self.fail("spool transition child hung")
            self.assertEqual(process.exitcode, 0)

        self.assertTrue(all(result[3] is None for result in results), results)
        self.assertEqual(sum(1 for result in results if result[1]), 1, results)
        final = self.spool.get_request(REQUEST_A)
        self.assertIn(final.state, {"claimed", "cancelled"})
        self.assertTrue(all(result[2] == final.state for result in results), results)

    def test_response_is_directly_readable_and_same_bytes_are_idempotent(self):
        self.publish()
        self.spool.claim_request(REQUEST_A)
        payload = request_payload(
            REQUEST_A, status="paused", reason="等待人工確認：整合衝突")

        first = self.spool.publish_response(REQUEST_A, payload)
        replay = self.spool.publish_response(
            REQUEST_A, dict(reversed(tuple(payload.items()))))
        direct = self.spool.get_response(REQUEST_A)

        self.assertTrue(first.created)
        self.assertFalse(replay.created)
        self.assertEqual(first.record.raw, replay.record.raw)
        self.assertEqual(direct.payload, payload)
        self.assertEqual(direct.raw, first.record.path.read_bytes())
        self.assertIn("等待人工確認", direct.raw.decode("utf-8"))

    def test_response_replay_with_different_bytes_conflicts(self):
        self.publish()
        self.spool.claim_request(REQUEST_A)
        self.spool.publish_response(
            REQUEST_A, request_payload(REQUEST_A, status="merged"))

        with self.assertRaises(spool_mod.SpoolConflictError):
            self.spool.publish_response(
                REQUEST_A, request_payload(REQUEST_A, status="fatal-invariant"))
        self.assertEqual(
            self.spool.get_response(REQUEST_A).payload["status"], "merged")

    def test_response_for_unknown_request_is_rejected(self):
        with self.assertRaises(spool_mod.SpoolNotFoundError):
            self.spool.publish_response(
                REQUEST_A, request_payload(REQUEST_A, status="merged"))
        self.assertFalse((self.responses / f"{REQUEST_A}.json").exists())

    def test_recovery_scan_enumerates_all_states_responses_and_staging(self):
        self.publish(REQUEST_A)
        self.publish(REQUEST_B)
        self.spool.claim_request(REQUEST_B)
        self.publish(REQUEST_C)
        self.spool.cancel_request(REQUEST_C)
        self.spool.publish_response(
            REQUEST_B, request_payload(REQUEST_B, status="merged"))
        staging = self.requests / "staging" / ("3" * 32)
        staging.mkdir()

        snapshot = self.spool.scan_recovery()

        self.assertEqual([item.request_id for item in snapshot.pending], [REQUEST_A])
        self.assertEqual([item.request_id for item in snapshot.claimed], [REQUEST_B])
        self.assertEqual([item.request_id for item in snapshot.cancelled], [REQUEST_C])
        self.assertEqual([item.request_id for item in snapshot.responses], [REQUEST_B])
        self.assertEqual(len(snapshot.staging), 1)
        self.assertFalse(snapshot.staging[0].complete)

    def test_malformed_json_duplicate_keys_and_filename_mismatch_fail_closed(self):
        pending = self.requests / "pending"
        (pending / f"{REQUEST_A}.json").write_bytes(b"{")
        with self.assertRaises(spool_mod.SpoolCorruptionError):
            self.spool.list_requests()

        (pending / f"{REQUEST_A}.json").write_text(
            '{"request_id":"' + REQUEST_A + '","request_id":"' + REQUEST_A + '"}',
            encoding="utf-8",
        )
        with self.assertRaises(spool_mod.SpoolCorruptionError):
            self.spool.get_request(REQUEST_A)

        (pending / f"{REQUEST_A}.json").write_text(
            json.dumps(request_payload(REQUEST_B)), encoding="utf-8")
        with self.assertRaises(spool_mod.SpoolCorruptionError):
            self.spool.scan_recovery()

    def test_filtered_enumeration_does_not_hide_duplicate_durable_state(self):
        record = self.publish()
        claimed = self.requests / "claimed" / f"{REQUEST_A}.json"
        claimed.write_bytes(record.raw)

        with self.assertRaises(spool_mod.SpoolCorruptionError):
            self.spool.list_requests("pending")

    def test_invalid_request_ids_never_become_paths(self):
        invalid = (
            "../" + REQUEST_A, "A" * 32, "a" * 31, "a" * 33,
            "a" * 16 + "/" + "b" * 15, "", None,
        )
        for request_id in invalid:
            with self.subTest(request_id=request_id):
                with self.assertRaises(spool_mod.InvalidRequestId):
                    self.spool.publish_request(
                        request_id, {"request_id": request_id})
                with self.assertRaises(spool_mod.InvalidRequestId):
                    self.spool.get_request(request_id)

    def test_payload_must_be_object_and_match_filename_id(self):
        with self.assertRaises(spool_mod.SpoolCorruptionError):
            self.spool.publish_request(REQUEST_A, {"request_id": REQUEST_B})
        with self.assertRaises(spool_mod.SpoolCorruptionError):
            self.spool.publish_request(REQUEST_A, [REQUEST_A])
        with self.assertRaises(spool_mod.SpoolCorruptionError):
            self.spool.publish_request(
                REQUEST_A, {"request_id": REQUEST_A, "bad": float("nan")})

    def test_symlink_artifact_is_rejected_without_reading_target(self):
        outside = self.root / "outside.json"
        outside.write_text(json.dumps(request_payload(REQUEST_A)), encoding="utf-8")
        link = self.requests / "pending" / f"{REQUEST_A}.json"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable:{exc}")

        with self.assertRaises(spool_mod.SpoolSecurityError):
            self.spool.get_request(REQUEST_A)
        self.assertEqual(
            json.loads(outside.read_text(encoding="utf-8"))["request_id"], REQUEST_A)

    def test_symlink_state_directory_is_rejected_before_publish(self):
        pending = self.requests / "pending"
        pending.rmdir()
        outside = self.root / "outside-dir"
        outside.mkdir()
        try:
            pending.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pending.mkdir()
            self.skipTest(f"directory symlink unavailable:{exc}")

        with self.assertRaises(spool_mod.SpoolSecurityError):
            self.publish()
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_ancestor_is_rejected_before_descendant_creation(self):
        outside = self.root / "outside-ancestor"
        outside.mkdir()
        linked_ancestor = self.root / "linked-ancestor"
        try:
            linked_ancestor.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable:{exc}")

        with self.assertRaises(spool_mod.SpoolSecurityError):
            spool_mod.DurableSpool(
                linked_ancestor / "nested" / "requests",
                responses_root=linked_ancestor / "nested" / "responses",
            )
        self.assertEqual(list(outside.iterdir()), [])

    def test_overlapping_response_tree_is_rejected_before_creating_spool(self):
        requests = self.root / "uncreated" / "requests"
        responses = requests / "pending" / "nested-responses"

        with self.assertRaises(spool_mod.SpoolSecurityError):
            spool_mod.DurableSpool(requests, responses_root=responses)

        self.assertFalse(requests.exists())

    def test_response_tree_cannot_replace_transition_lock_before_creation(self):
        requests = self.root / "uncreated-lock-overlap" / "requests"
        responses = requests / spool_mod._LOCK_FILE

        with self.assertRaises(spool_mod.SpoolSecurityError):
            spool_mod.DurableSpool(requests, responses_root=responses)

        self.assertFalse(requests.exists())

    def test_reparse_ancestor_check_precedes_descendant_creation(self):
        """Exercise the ancestor guard even where creating symlinks is denied."""
        ancestor = self.root / "emulated-reparse"
        ancestor.mkdir()
        identity = (ancestor.lstat().st_dev, ancestor.lstat().st_ino)
        real_is_link = spool_mod._is_link

        def emulated_link(info):
            if (info.st_dev, info.st_ino) == identity:
                return True
            return real_is_link(info)

        with mock.patch.object(
                spool_mod, "_is_link", side_effect=emulated_link):
            with self.assertRaises(spool_mod.SpoolSecurityError):
                spool_mod.DurableSpool(ancestor / "nested" / "requests")
        self.assertFalse((ancestor / "nested").exists())

    def test_hardlinked_artifact_is_rejected(self):
        outside = self.root / "outside-hardlink.json"
        outside.write_text(json.dumps(request_payload(REQUEST_A)), encoding="utf-8")
        target = self.requests / "pending" / f"{REQUEST_A}.json"
        try:
            os.link(outside, target)
        except OSError as exc:
            self.skipTest(f"hardlink unavailable:{exc}")
        with self.assertRaises(spool_mod.SpoolSecurityError):
            self.spool.list_requests()

    def test_unknown_and_orphan_artifacts_make_recovery_fail_closed(self):
        (self.requests / "pending" / "unexpected.tmp").write_text("x", encoding="utf-8")
        with self.assertRaises(spool_mod.SpoolCorruptionError):
            self.spool.scan_recovery()
        (self.requests / "pending" / "unexpected.tmp").unlink()

        (self.responses / f"{REQUEST_D}.json").write_text(
            json.dumps(request_payload(REQUEST_D, status="merged")), encoding="utf-8")
        with self.assertRaises(spool_mod.SpoolCorruptionError):
            self.spool.list_responses()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    unittest.main()

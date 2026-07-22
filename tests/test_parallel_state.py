"""Durable parallel artifact and state-machine contract tests."""

import copy
import os
import stat
import tempfile
import unittest
from pathlib import Path

from engine import parallel_state as state


RUN_ID = "a1b2c3d4"
START_SHA = "1" * 40
SHA_A = "2" * 40
SHA_B = "3" * 40


def frozen_plan():
    return [
        {"order": 1, "task": "first", "ref": None, "stack": 7},
        {"order": 2, "task": "second", "ref": "PLAN.md#two", "stack": 7},
        {"order": 3, "task": "third", "ref": None},
        {"order": 4, "task": "fourth", "ref": None, "stack": 8},
    ]


def run_config(repo: Path):
    return {
        "repo": str(repo.resolve()),
        "primary_repo": str(repo.resolve()),
        "goal": "goal.md",
        "plan_doc": "PLAN.md",
        "agent_cmd": "agent --flag",
        "validate_cmd": "validate",
        "flag_threshold": 3,
        "done_threshold": 2,
        "red_limit": 2,
        "stall_limit": 4,
        "stuck_stop": True,
        "stuck_stop_count": 2,
        "round_timeout": 0,
        "validate_timeout": 30,
        "agent_backoff_max": 5,
        "notify_cmd": "",
        "max_parallel": 2,
        "worker_restart_limit": 3,
        "environment": {
            "path_additions": [str((repo / "tools" / "bin").resolve())],
            "non_secret": {"MODE": "test", "TRACE": False},
            "required_secret_names": ["API_TOKEN"],
        },
    }


class TempRootTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()


class TestCanonicalArtifactIO(TempRootTest):
    def test_canonical_hash_is_order_independent_and_rejects_nonfinite(self):
        left = {"z": "中文", "a": [1, True, None]}
        right = {"a": [1, True, None], "z": "中文"}
        self.assertEqual(state.canonical_json_bytes(left), state.canonical_json_bytes(right))
        self.assertEqual(state.canonical_json_hash(left), state.canonical_json_hash(right))
        self.assertEqual(len(state.canonical_json_hash(left)), 64)
        with self.assertRaises(state.ParallelStateError):
            state.canonical_json_bytes({"bad": float("nan")})
        with self.assertRaisesRegex(state.ParallelStateError, "keys"):
            state.canonical_json_bytes({1: "ambiguous", "1": "collision"})

    def test_immutable_write_is_atomic_idempotent_and_byte_strict(self):
        value = {"b": 2, "a": "é"}
        path = state.write_or_verify_immutable_json(self.root, "run/plan.json", value)
        self.assertEqual(path.read_bytes(), state.canonical_json_bytes(value) + b"\n")
        state.write_or_verify_immutable_json(self.root, "run/plan.json", copy.deepcopy(value))
        with self.assertRaisesRegex(state.ParallelStateError, "different bytes"):
            state.write_or_verify_immutable_json(self.root, "run/plan.json", {"a": "é"})
        self.assertFalse(any(path.parent.glob(".parallel-state-*.tmp")))

    def test_read_requires_canonical_json_and_rejects_duplicate_keys(self):
        run = self.root / "run"
        run.mkdir()
        artifact = run / "pretty.json"
        artifact.write_bytes(b'{"a": 1}\n')
        with self.assertRaisesRegex(state.ParallelStateError, "not canonical"):
            state.read_canonical_json(run, "pretty.json")
        artifact.write_bytes(b'{"a":1,"a":2}\n')
        with self.assertRaisesRegex(state.ParallelStateError, "duplicate"):
            state.read_canonical_json(run, "pretty.json")

    def test_mutable_json_replace_remains_canonical(self):
        state.atomic_write_json(self.root, "run/aggregate.json", {"generation": 1})
        state.atomic_write_json(self.root, "run/aggregate.json", {"generation": 2})
        self.assertEqual(
            state.read_canonical_json(self.root, "run/aggregate.json"),
            {"generation": 2},
        )

    def test_path_traversal_nonregular_and_linked_parent_fail_closed(self):
        with self.assertRaises(state.ParallelStateError):
            state.atomic_write_json(self.root, "../escape.json", {})
        run = self.root / "run"
        run.mkdir()
        (run / "as-file.json").mkdir()
        with self.assertRaises(state.ParallelStateError):
            state.atomic_write_json(run, "as-file.json", {})

        outside = self.root / "outside"
        outside.mkdir()
        linked = self.root / "linked"
        try:
            os.symlink(outside, linked, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")
        with self.assertRaises(state.ParallelStateError):
            state.atomic_write_json(self.root, "linked/pwn.json", {})
        with self.assertRaises(state.ParallelStateError):
            state.derive_run_directory(self.root, "linked", RUN_ID)

    def test_environment_contract_is_absolute_disjoint_and_runtime_safe(self):
        addition = (self.root / "tools" / "bin").resolve()
        contract = state.normalize_environment_contract({
            "path_additions": [str(addition), str(addition)],
            "non_secret": {"MODE": "test", "TRACE": False},
            "required_secret_names": ["SERVICE_TOKEN", "API_TOKEN"],
        })
        self.assertEqual(contract["path_additions"], [str(addition)])
        self.assertEqual(
            contract["required_secret_names"], ["API_TOKEN", "SERVICE_TOKEN"])

        invalid_contracts = (
            ({"BAD=NAME": "x"}, [], "portable"),
            ({"PATH": "x"}, [], "reserved"),
            ({"LOOP_WS": "x"}, [], "reserved"),
            ({"API_TOKEN": "value"}, ["api_token"], "disjoint"),
            ({"MODE": "x", "mode": "y"}, [], "case-insensitively unique"),
        )
        for non_secret, required, message in invalid_contracts:
            with self.subTest(non_secret=non_secret, required=required), \
                    self.assertRaisesRegex(state.ParallelStateError, message):
                state.normalize_environment_contract({
                    "path_additions": [],
                    "non_secret": non_secret,
                    "required_secret_names": required,
                })
        with self.assertRaisesRegex(state.ParallelStateError, "absolute"):
            state.normalize_environment_contract({
                "path_additions": ["relative/bin"],
                "non_secret": {},
                "required_secret_names": [],
            })


class TestDerivationAndBatches(TempRootTest):
    def test_canonical_run_ref_worktree_and_workspace_derivation(self):
        repo = self.root / "repo"
        repo.mkdir()
        run_dir = state.derive_run_directory(self.root, "base", RUN_ID)
        identity = state.derive_task_identity(
            self.root, "base", RUN_ID, 3, target_repo=repo)
        self.assertEqual(run_dir, self.root / "base" / "parallel" / RUN_ID)
        self.assertEqual(
            identity.integration_ref,
            f"refs/heads/loop/{RUN_ID}/integration",
        )
        self.assertEqual(identity.task_ref, f"refs/heads/loop/{RUN_ID}/task-3")
        self.assertEqual(
            identity.worktree_path,
            self.root / "base" / "worktrees" / f"{RUN_ID}-task-3",
        )
        self.assertEqual(identity.worker_workspace, f"base--{RUN_ID}-task-3")
        self.assertEqual(
            identity.worker_workspace_path,
            self.root / f"base--{RUN_ID}-task-3",
        )

    def test_worktree_inside_target_repo_is_rejected(self):
        with self.assertRaisesRegex(state.ParallelStateError, "outside"):
            state.derive_task_identity(
                self.root, "base", RUN_ID, 1, target_repo=self.root)

    def test_stack_projection_preserves_contiguous_batch_order(self):
        self.assertEqual(state.project_stack_batches(frozen_plan()), [
            {"index": 1, "stack": 7, "orders": [1, 2]},
            {"index": 2, "stack": None, "orders": [3]},
            {"index": 3, "stack": 8, "orders": [4]},
        ])
        invalid = frozen_plan()
        invalid.append({"order": 5, "task": "repeat", "ref": None, "stack": 7})
        with self.assertRaises(state.ParallelStateError):
            state.project_stack_batches(invalid)


class TestRunArtifacts(TempRootTest):
    def setUp(self):
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.tokens = {order: f"dispatch-token-for-task-{order}" for order in range(1, 5)}

    def materialize(self, **updates):
        arguments = {
            "workspace_root": self.root,
            "parent_workspace": "base",
            "run_id": RUN_ID,
            "plan": frozen_plan(),
            "run_config": run_config(self.repo),
            "integration_start_sha": START_SHA,
            "integration_branch": "main",
            "gate_client_cmd": "python -m engine.parallel_gate complete",
            "dispatch_tokens": self.tokens,
        }
        arguments.update(updates)
        return state.materialize_run_artifacts(**arguments)

    def test_fresh_materialization_round_trip_and_secret_separation(self):
        artifacts = self.materialize()
        self.assertEqual(artifacts.plan_hash, state.canonical_json_hash(frozen_plan()))
        self.assertEqual(artifacts.dispatch_tokens, self.tokens)
        self.assertEqual(set(artifacts.assignments), {1, 2, 3, 4})
        self.assertEqual(artifacts.assignments[1]["batch_index"], 1)
        self.assertEqual(artifacts.assignments[3]["batch_index"], 2)
        token_path = artifacts.run_dir / "dispatch" / "task-1.token"
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        else:
            # DOS st_mode bits do not represent the effective Windows ACL.
            self.assertTrue(token_path.is_file())
        self.assertEqual(
            state.read_dispatch_token(
                artifacts.run_dir,
                1,
                expected_hash=artifacts.assignments[1]["dispatch_token_hash"],
            ),
            self.tokens[1],
        )
        immutable_parts = [
            (artifacts.run_dir / "manifest.json").read_bytes(),
            (artifacts.run_dir / "run-config.json").read_bytes(),
        ]
        immutable_parts.extend(
            (artifacts.run_dir / "assignments" / f"task-{order}.json").read_bytes()
            for order in range(1, 5)
        )
        immutable_bytes = b"".join(immutable_parts)
        for token in self.tokens.values():
            self.assertNotIn(token.encode(), immutable_bytes)
        validated = state.validate_run_artifacts(
            artifacts.run_dir, workspace_root=self.root)
        self.assertEqual(validated.manifest_hash, artifacts.manifest_hash)
        self.assertEqual(validated.dispatch_tokens, {})

    def test_idempotent_materialization_and_generated_token_recovery(self):
        generated = self.materialize(dispatch_tokens=None)
        again = self.materialize(dispatch_tokens=None)
        self.assertEqual(generated.assignment_hashes, again.assignment_hashes)
        self.assertEqual(generated.dispatch_tokens, again.dispatch_tokens)
        self.assertEqual(len(set(generated.dispatch_tokens.values())), 4)

    def test_manifest_marks_complete_graph_and_missing_child_is_not_healed(self):
        artifacts = self.materialize()
        assignment = artifacts.run_dir / "assignments" / "task-2.json"
        assignment.unlink()
        with self.assertRaises(state.ParallelStateError):
            self.materialize()
        self.assertFalse(assignment.exists())

    def test_cross_hash_and_dispatch_hash_tampering_fail_closed(self):
        artifacts = self.materialize()
        assignment_path = artifacts.run_dir / "assignments" / "task-1.json"
        assignment = dict(state.read_canonical_json(
            artifacts.run_dir, "assignments/task-1.json"))
        assignment["dispatch_token_hash"] = "f" * 64
        assignment_path.write_bytes(state.canonical_json_bytes(assignment) + b"\n")
        with self.assertRaises(state.ParallelStateError):
            state.validate_run_artifacts(artifacts.run_dir, workspace_root=self.root)

    def test_validator_does_not_read_secret_but_supervisor_resume_does(self):
        artifacts = self.materialize()
        token_path = artifacts.run_dir / "dispatch" / "task-3.token"
        token_path.unlink()
        state.validate_run_artifacts(artifacts.run_dir, workspace_root=self.root)
        with self.assertRaisesRegex(state.ParallelStateError, "unavailable"):
            self.materialize(dispatch_tokens=None)

    def test_run_config_rejects_secret_or_unknown_fields(self):
        config = run_config(self.repo)
        config["api_token"] = "do-not-persist"
        with self.assertRaisesRegex(state.ParallelStateError, "extra"):
            state.normalize_run_config(config)

    def test_known_stale_atomic_temp_has_no_authority_but_other_extra_file_fails(self):
        artifacts = self.materialize()
        staging = artifacts.run_dir / "assignments" / (
            ".parallel-state-" + "a" * 32 + ".tmp")
        staging.write_bytes(b"unpublished")
        state.validate_run_artifacts(artifacts.run_dir, workspace_root=self.root)
        (artifacts.run_dir / "assignments" / "task-999.json").write_bytes(b"{}\n")
        with self.assertRaisesRegex(state.ParallelStateError, "files mismatch"):
            state.validate_run_artifacts(artifacts.run_dir, workspace_root=self.root)


class TestAggregateState(unittest.TestCase):
    def initial(self):
        return state.build_initial_aggregate(RUN_ID, frozen_plan())

    def test_completion_and_cancellation_require_monotonic_intent(self):
        aggregate = state.transition_run_status(self.initial(), "running")
        with self.assertRaises(state.ParallelStateError):
            state.transition_run_status(aggregate, "finalizing")
        for task in list(aggregate["tasks"]):
            order = task["order"]
            aggregate = state.transition_task(
                aggregate, order, resource_state="provisioning")
            aggregate = state.transition_task(
                aggregate, order, resource_state="running")
            aggregate = state.transition_task(
                aggregate, order, outcome="integrated", resource_state="exited")
            aggregate = state.transition_task(
                aggregate, order, resource_state="cleaning")
            aggregate = state.transition_task(
                aggregate, order, resource_state="cleaned")
        aggregate = state.set_terminal_intent(aggregate, "completed")
        aggregate = state.transition_run_status(aggregate, "finalizing")
        aggregate = state.transition_run_status(aggregate, "completed")
        self.assertEqual(aggregate["status"], "completed")
        with self.assertRaises(state.ParallelStateError):
            state.set_terminal_intent(aggregate, "cancelled")
        with self.assertRaises(state.ParallelStateError):
            state.transition_run_status(aggregate, "cancel_requested")

        cancelled = state.set_terminal_intent(self.initial(), "cancelled")
        for task in list(cancelled["tasks"]):
            cancelled = state.transition_task(
                cancelled, task["order"], outcome="cancelled",
                resource_state="cleaned", explicit_abort=True)
        cancelled = state.transition_run_status(cancelled, "cancel_requested")
        cancelled = state.transition_run_status(cancelled, "finalizing_cancel")
        cancelled = state.transition_run_status(cancelled, "cancelled")
        self.assertEqual(cancelled["terminal_intent"], "cancelled")

    def test_blocked_resume_depends_on_terminal_intent(self):
        plain = state.transition_run_status(self.initial(), "blocked")
        self.assertEqual(
            state.transition_run_status(plain, "initializing")["status"],
            "initializing",
        )
        completing = state.set_terminal_intent(self.initial(), "completed")
        completing = state.transition_run_status(completing, "blocked")
        self.assertEqual(
            state.transition_run_status(completing, "finalizing")["status"],
            "finalizing",
        )
        with self.assertRaises(state.ParallelStateError):
            state.transition_run_status(completing, "initializing")

    def test_outcome_and_resource_lifecycles_remain_independent(self):
        aggregate = self.initial()
        with self.assertRaisesRegex(state.ParallelStateError, "explicit Abort"):
            state.transition_task(aggregate, 1, outcome="cancelled")
        cancelled = state.transition_task(
            aggregate,
            1,
            outcome="cancelled",
            resource_state="cleaned",
            explicit_abort=True,
        )
        self.assertEqual(cancelled["tasks"][0]["outcome"], "cancelled")
        self.assertEqual(cancelled["tasks"][0]["resource_state"], "cleaned")

        integrated = state.transition_task(aggregate, 1, resource_state="provisioning")
        integrated = state.transition_task(integrated, 1, resource_state="running")
        integrated = state.transition_task(integrated, 1, resource_state="gate_pending")
        integrated = state.transition_task(integrated, 1, resource_state="gate_claimed")
        integrated = state.transition_task(integrated, 1, outcome="integrated")
        integrated = state.transition_task(integrated, 1, resource_state="exited")
        integrated = state.transition_task(integrated, 1, resource_state="cleaning")
        integrated = state.transition_task(integrated, 1, resource_state="cleanup_failed")
        self.assertEqual(integrated["tasks"][0]["outcome"], "integrated")
        with self.assertRaises(state.ParallelStateError):
            state.transition_task(integrated, 1, outcome="cancelled", explicit_abort=True)
        with self.assertRaisesRegex(state.ParallelStateError, "cleanup retry"):
            state.transition_task(integrated, 1, resource_state="cleaning")
        retried = state.transition_task(
            integrated, 1, resource_state="cleaning", cleanup_retry=True)
        self.assertEqual(retried["tasks"][0]["resource_state"], "cleaning")

    def test_resume_pause_restart_and_gate_claim_rules(self):
        aggregate = self.initial()
        for target in ("provisioning", "running", "pausing", "paused"):
            aggregate = state.transition_task(aggregate, 1, resource_state=target)
        with self.assertRaisesRegex(state.ParallelStateError, "explicit Resume"):
            state.transition_task(aggregate, 1, resource_state="provisioning")
        aggregate = state.transition_task(
            aggregate, 1, resource_state="provisioning", explicit_resume=True)
        aggregate = state.transition_task(aggregate, 1, resource_state="running")
        aggregate = state.transition_task(aggregate, 1, resource_state="gate_pending")
        aggregate = state.transition_task(aggregate, 1, resource_state="gate_claimed")
        with self.assertRaises(state.ParallelStateError):
            state.transition_task(aggregate, 1, resource_state="pausing")
        stale = state.transition_task(aggregate, 1, resource_state="running")
        self.assertEqual(stale["tasks"][0]["resource_state"], "running")
        terminal = copy.deepcopy(aggregate)
        terminal["tasks"][0]["outcome"] = "integrated"
        with self.assertRaisesRegex(state.ParallelStateError, "reactivate"):
            state.transition_task(terminal, 1, resource_state="running")

        advanced = state.advance_pause_generation(aggregate)
        self.assertEqual(advanced["pause_generation"], 1)
        restarted = state.increment_restart_count(advanced, 2, limit=1)
        self.assertEqual(restarted["tasks"][1]["restart_count"], 1)
        with self.assertRaisesRegex(state.ParallelStateError, "limit"):
            state.increment_restart_count(restarted, 2, limit=1)

    def test_unknown_enum_and_worker_projection_are_rejected(self):
        aggregate = self.initial()
        aggregate["tasks"][0]["resource_state"] = "integrated"
        with self.assertRaises(state.ParallelStateError):
            state.validate_aggregate(aggregate)
        self.assertEqual(
            state.require_worker_assignment_status("recovery-required"),
            "recovery-required",
        )
        with self.assertRaises(state.ParallelStateError):
            state.require_worker_assignment_status("gate_claimed")


class TestReceiptProjection(TestRunArtifacts):
    def receipt(
        self,
        artifacts,
        *,
        task,
        sequence,
        before,
        after,
        previous,
    ):
        return {
            "schema_version": state.SCHEMA_VERSION,
            "run_id": RUN_ID,
            "manifest_hash": artifacts.manifest_hash,
            "assignment_hash": artifacts.assignment_hashes[task],
            "task": task,
            "request_id": f"{sequence:032x}",
            "sequence": sequence,
            "previous_receipt_hash": previous,
            "integration_before": before,
            "validated_sha": after,
            "validated_round": sequence + 10,
        }

    def test_receipt_chain_can_merge_batch_out_of_plan_order_but_projects_in_order(self):
        artifacts = self.materialize()
        first = self.receipt(
            artifacts,
            task=2,
            sequence=1,
            before=START_SHA,
            after=SHA_A,
            previous=None,
        )
        second = self.receipt(
            artifacts,
            task=1,
            sequence=2,
            before=SHA_A,
            after=SHA_B,
            previous=state.canonical_json_hash(first),
        )
        chain = state.validate_receipt_chain([second, first], artifacts)
        self.assertEqual([item["task"] for item in chain], [2, 1])
        self.assertEqual(state.project_completed_from_receipts(chain, artifacts), [
            {"order": 1, "base_sha": SHA_A, "sha": SHA_B, "round": 12},
            {"order": 2, "base_sha": START_SHA, "sha": SHA_A, "round": 11},
        ])

        state.write_or_verify_immutable_json(
            artifacts.run_dir, "receipts/task-2.json", first)
        state.write_or_verify_immutable_json(
            artifacts.run_dir, "receipts/task-1.json", second)
        loaded_artifacts, loaded_chain = state.load_receipt_chain(
            artifacts.run_dir, workspace_root=self.root)
        self.assertEqual(loaded_artifacts.manifest_hash, artifacts.manifest_hash)
        self.assertEqual(loaded_chain, chain)

    def test_broken_receipt_hash_integration_and_authority_are_rejected(self):
        artifacts = self.materialize()
        first = self.receipt(
            artifacts,
            task=1,
            sequence=1,
            before=START_SHA,
            after=SHA_A,
            previous=None,
        )
        cases = []
        broken_authority = copy.deepcopy(first)
        broken_authority["assignment_hash"] = "f" * 64
        cases.append([broken_authority])
        broken_before = copy.deepcopy(first)
        broken_before["integration_before"] = SHA_B
        cases.append([broken_before])
        second = self.receipt(
            artifacts,
            task=2,
            sequence=2,
            before=SHA_A,
            after=SHA_B,
            previous="f" * 64,
        )
        cases.append([first, second])
        duplicate = copy.deepcopy(first)
        duplicate["sequence"] = 2
        duplicate["request_id"] = "2" * 32
        duplicate["previous_receipt_hash"] = state.canonical_json_hash(first)
        duplicate["integration_before"] = SHA_A
        duplicate["validated_sha"] = SHA_B
        cases.append([first, duplicate])
        for receipts in cases:
            with self.subTest(receipts=receipts), self.assertRaises(state.ParallelStateError):
                state.validate_receipt_chain(receipts, artifacts)


if __name__ == "__main__":
    unittest.main()

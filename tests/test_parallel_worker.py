"""Pure managed-worker argv/state contract tests."""

import argparse
import copy
import unittest

from engine import parallel_contract as contract
from engine import parallel_worker as worker


RUN_ID = "a1b2c3d4"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
INTEGRATION_REF = f"refs/heads/loop/{RUN_ID}/integration"
TASK_REF = f"refs/heads/loop/{RUN_ID}/task-3"


class RaisingParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def build_parser():
    parser = RaisingParser()
    parser.add_argument("--import-plan", default="")
    parser.add_argument("--start-phase", choices=("plan", "exec"), default="plan")
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--resume-interrupted", action="store_true")
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--consume-import-plan", action="store_true")
    parser.add_argument("--max-rounds", type=int, default=0)
    worker.add_arguments(parser)
    return parser


def worker_argv(*, resume=False):
    argv = [
        "--start-task", "3",
        "--stop-after-task",
        "--complete-gate-cmd", "python -m engine.gate_client",
        "--integration-ref", INTEGRATION_REF,
        "--parent-workspace", "base",
        "--task-ref", TASK_REF,
        "--run-config-hash", HASH_A,
        "--launch-spec-hash", HASH_B,
        "--manifest-hash", HASH_C,
    ]
    if resume:
        argv.append("--managed-worker-resume")
    else:
        argv.extend(("--import-plan", "plan.json", "--start-phase", "exec"))
    return argv


def launch(*, resume=False, updates=()):
    parser = build_parser()
    args = parser.parse_args([*worker_argv(resume=resume), *updates])
    return worker.validate_launch_args(parser, args)


def base_state():
    return {
        "phase": "exec",
        "plan": [
            {"order": 1, "task": "first", "ref": None},
            {"order": 2, "task": "second", "ref": None, "stack": 7},
            {"order": 3, "task": "assigned", "ref": None, "stack": 7},
        ],
        "completed": [],
    }


class TestManagedWorkerLaunchArgs(unittest.TestCase):
    def test_no_worker_flags_returns_none(self):
        parser = build_parser()
        args = parser.parse_args([])
        self.assertIsNone(worker.validate_launch_args(parser, args))

    def test_initial_launch_derives_run_and_task_identity(self):
        value = launch()
        self.assertFalse(value.resume)
        self.assertEqual(value.run_id, RUN_ID)
        self.assertEqual(value.assigned_order, 3)
        self.assertEqual(value.integration_ref, INTEGRATION_REF)
        self.assertEqual(value.task_ref, worker.task_ref_for(RUN_ID, 3))
        self.assertEqual(value.run_config_hash, HASH_A)

    def test_partial_worker_flags_are_rejected_as_one_group(self):
        parser = build_parser()
        args = parser.parse_args(["--start-task", "3"])
        with self.assertRaisesRegex(ValueError, "必須整組提供"):
            worker.validate_launch_args(parser, args)

    def test_initial_launch_requires_import_and_exec_without_other_modes(self):
        cases = (
            [item for item in worker_argv() if item not in ("--import-plan", "plan.json")],
            [*worker_argv(), "--start-phase", "plan"],
            [*worker_argv(), "--reset-state"],
            [*worker_argv(), "--resume-interrupted"],
            [*worker_argv(), "--init-only"],
            [*worker_argv(), "--preflight-only"],
            [*worker_argv(), "--consume-import-plan"],
            [*worker_argv(), "--max-rounds", "1"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                parser = build_parser()
                args = parser.parse_args(argv)
                with self.assertRaises(ValueError):
                    worker.validate_launch_args(parser, args)

    def test_resume_forbids_fresh_or_unsafe_modes(self):
        forbidden = (
            ("--import-plan", "plan.json"),
            ("--reset-state",),
            ("--resume-interrupted",),
            ("--init-only",),
            ("--preflight-only",),
            ("--consume-import-plan",),
            ("--max-rounds", "1"),
        )
        for extra in forbidden:
            with self.subTest(extra=extra):
                parser = build_parser()
                args = parser.parse_args([*worker_argv(resume=True), *extra])
                with self.assertRaisesRegex(ValueError, "--managed-worker-resume"):
                    worker.validate_launch_args(parser, args)

    def test_ref_hash_parent_and_order_injection_are_rejected(self):
        cases = (
            ("--start-task", "0"),
            ("--integration-ref", INTEGRATION_REF + ";evil"),
            ("--task-ref", f"refs/heads/loop/{RUN_ID}/task-4"),
            ("--run-config-hash", "A" * 64),
            ("--launch-spec-hash", "b" * 63),
            ("--manifest-hash", "not-a-hash"),
            ("--parent-workspace", "../base"),
        )
        for option, invalid in cases:
            with self.subTest(option=option, invalid=invalid):
                argv = worker_argv()
                index = argv.index(option)
                argv[index + 1] = invalid
                parser = build_parser()
                args = parser.parse_args(argv)
                with self.assertRaises(ValueError):
                    worker.validate_launch_args(parser, args)


class TestManagedWorkerState(unittest.TestCase):
    def test_initialize_state_is_pure_and_saves_frozen_contract(self):
        source = base_state()
        before = copy.deepcopy(source)

        state = worker.initialize_state(source, launch())

        self.assertEqual(source, before)
        self.assertEqual(state["runner"], "parallel-worker")
        self.assertIs(state["managed_readonly"], True)
        self.assertEqual(state["parent_workspace"], "base")
        self.assertEqual(state["run_id"], RUN_ID)
        self.assertEqual(state["assigned_order"], 3)
        self.assertEqual(state["current_order"], 3)
        self.assertEqual(state["task_ref"], TASK_REF)
        self.assertEqual(state["integration_ref"], INTEGRATION_REF)
        self.assertEqual(state["assignment"], {
            "status": "running",
            "validated_sha": None,
            "validated_round": None,
            "exit_reason": None,
            "pause_generation": 0,
            "gate_request": None,
        })

    def test_initialize_rejects_missing_order_or_task_description(self):
        cases = (
            {"phase": "exec", "plan": [{"order": 1, "task": "other"}]},
            {"phase": "exec", "plan": [{"order": 3}]},
            {"phase": "exec", "plan": [{"order": True, "task": "bool is not 1"}]},
        )
        for state in cases:
            with self.subTest(state=state), self.assertRaises(contract.ParallelContractError):
                worker.initialize_state(state, launch())

    def test_resume_accepts_exact_state_without_mutation(self):
        state = worker.initialize_state(base_state(), launch())
        before = copy.deepcopy(state)

        self.assertIsNone(worker.validate_resume_state(state, launch(resume=True)))
        self.assertEqual(state, before)

    def test_resume_rejects_identity_config_and_assignment_drift(self):
        initial = worker.initialize_state(base_state(), launch())
        drifts = {
            "runner": lambda state: state.__setitem__("runner", "loop"),
            "managed_readonly": lambda state: state.__setitem__("managed_readonly", False),
            "parent": lambda state: state.__setitem__("parent_workspace", "other"),
            "run_id": lambda state: state.__setitem__("run_id", "deadbeef"),
            "order": lambda state: state.__setitem__("assigned_order", 1),
            "current_order": lambda state: state.__setitem__("current_order", 2),
            "task_ref": lambda state: state.__setitem__("task_ref", TASK_REF + "-other"),
            "integration_ref": lambda state: state.__setitem__("integration_ref", "refs/heads/main"),
            "run_hash": lambda state: state.__setitem__("run_config_hash", HASH_B),
            "launch_hash": lambda state: state.__setitem__("launch_spec_hash", HASH_A),
            "manifest_hash": lambda state: state.__setitem__("manifest_hash", HASH_A),
            "gate": lambda state: state.__setitem__("complete_gate_cmd", "other gate"),
            "status": lambda state: state["assignment"].__setitem__("status", "blocked"),
            "exit_reason": lambda state: state["assignment"].__setitem__("exit_reason", "fatal"),
            "gate_request": lambda state: state["assignment"].__setitem__("gate_request", {
                "request_id": "1" * 32,
                "validated_sha": "a" * 40,
                "validated_round": 1,
            }),
            "orphan_validated": lambda state: state["assignment"].update({
                "validated_sha": "a" * 40,
                "validated_round": 1,
            }),
        }
        resume_launch = launch(resume=True)
        for label, mutate in drifts.items():
            with self.subTest(label=label):
                state = copy.deepcopy(initial)
                mutate(state)
                with self.assertRaises(contract.ParallelContractError):
                    worker.validate_resume_state(state, resume_launch)

    def test_resume_rejects_missing_assigned_task(self):
        state = worker.initialize_state(base_state(), launch())
        state["plan"] = [{"order": 1, "task": "only other task"}]
        with self.assertRaisesRegex(contract.ParallelContractError, "assigned_order 3"):
            worker.validate_resume_state(state, launch(resume=True))

    def test_persisted_state_accepts_running_and_rejects_terminal_schema_drift(self):
        state = worker.initialize_state(base_state(), launch())
        worker.validate_persisted_state(state)

        terminal = copy.deepcopy(state)
        terminal["assignment"].update({
            "status": "integrated",
            "validated_sha": "a" * 40,
            "validated_round": 4,
        })
        worker.validate_persisted_state(terminal)

        pending = copy.deepcopy(state)
        pending["assignment"].update({
            "validated_sha": "a" * 40,
            "validated_round": 4,
            "gate_request": {
                "request_id": "1" * 32,
                "validated_sha": "a" * 40,
                "validated_round": 4,
            },
        })
        worker.validate_persisted_state(pending)
        with self.assertRaises(contract.ParallelContractError):
            worker.validate_resume_state(pending, launch(resume=True))

        orphaned = copy.deepcopy(state)
        orphaned["assignment"].update({
            "validated_sha": "a" * 40,
            "validated_round": 4,
        })
        with self.assertRaises(contract.ParallelContractError):
            worker.validate_persisted_state(orphaned)

        mutations = (
            lambda value: value["assignment"].update(status="unknown"),
            lambda value: value["assignment"].update(
                status="integrated", validated_sha=None, validated_round=None),
            lambda value: value.update(task_ref="refs/heads/other"),
            lambda value: value["assignment"].update(
                status="blocked", exit_reason=None),
        )
        for mutate in mutations:
            broken = copy.deepcopy(terminal)
            mutate(broken)
            with self.subTest(state=broken), self.assertRaises(
                    contract.ParallelContractError):
                worker.validate_persisted_state(broken)


if __name__ == "__main__":
    unittest.main()

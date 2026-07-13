#!/usr/bin/env python3
"""Fleet vertical-slice integration tests with real Git worktrees and fake agents."""
import json
import fcntl
import hashlib
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from engine import dashboard as D
from engine import fleet as F


ROOT = Path(__file__).resolve().parent.parent
FAKE_AGENT = ROOT / "tests" / "fleet_fake_agent.py"
INTEGRATION_VALIDATE = ROOT / "tests" / "fleet_validate.py"


class TestFleetHappyPath(unittest.TestCase):
    def test_track_env_rejects_credentials_that_would_be_persisted(self):
        with self.assertRaisesRegex(ValueError, "credential"):
            F.validate_track_env({"GITHUB_TOKEN": "secret"})
        self.assertEqual(
            F.validate_track_env({"SERVICE_URL": "http://127.0.0.1:{port}/{safe_track}"}),
            {"SERVICE_URL": "http://127.0.0.1:{port}/{safe_track}"})
        for key in ("LOOP_AGENT_WORKSPACE_ROOT", "GIT_DIR", "PATH", "PYTHONHOME",
                    "NODE_OPTIONS", "CODEX_HOME"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "coordinator/runtime"):
                F.validate_track_env({key: "/tmp/escape"})

    def make_repo(self, temp: Path) -> Path:
        repo = temp / "repo"
        repo.mkdir()
        for command in (["git", "init", "-q"],
                        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                        ["git", "config", "user.email", "fleet@test.local"],
                        ["git", "config", "user.name", "fleet-test"]):
            subprocess.run(command, cwd=repo, check=True)
        (repo / "goal.md").write_text("Implement both tracks\n", encoding="utf-8")
        subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
        return repo

    def fleet_env(self, temp: Path, **extra) -> dict:
        env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(temp / "workspace"), **extra}
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return env

    def fleet_command(self, repo: Path, *, name="parallel", validate_cmd="true",
                      import_plan: Path | None = None, max_parallel=2) -> list[str]:
        command = [
            sys.executable, "-m", "engine.fleet", "--repo", str(repo), "--name", name,
            "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
            "--validate-cmd", validate_cmd, "--flag-threshold", "1", "--done-threshold", "1",
            "--merge-threshold", "1", "--max-parallel", str(max_parallel),
            "--round-timeout", "1",
        ]
        if import_plan is not None:
            command += ["--import-plan", str(import_plan)]
        return command

    def write_plan(self, temp: Path, tasks, name="plan.json") -> Path:
        path = temp / name
        path.write_text(json.dumps(tasks), encoding="utf-8")
        return path

    def wait_for_fleet(self, path: Path, predicate, *, timeout=15):
        deadline = time.monotonic() + timeout
        latest = {}
        while time.monotonic() < deadline:
            try:
                latest = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if predicate(latest):
                return latest
            time.sleep(0.05)
        self.fail(f"fleet state did not reach expected condition: {latest}")

    @staticmethod
    def git_head(repo: Path) -> str:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                              text=True, capture_output=True).stdout.strip()

    @staticmethod
    def file_lock_is_held(path: Path) -> bool:
        with path.open("a+b") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            return False

    def crash_case(self, crash_at, *, rollback=False, resume=True):
        temp_dir = tempfile.TemporaryDirectory()
        temp = Path(temp_dir.name)
        repo = temp / "repo"
        repo.mkdir()
        for command in (["git", "init", "-q"],
                        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                        ["git", "config", "user.email", "fleet@test.local"],
                        ["git", "config", "user.name", "fleet-test"]):
            subprocess.run(command, cwd=repo, check=True)
        (repo / "goal.md").write_text("Implement both tracks\n", encoding="utf-8")
        subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
        env = dict(os.environ)
        env["LOOP_AGENT_WORKSPACE_ROOT"] = str(temp / "workspace")
        env["LOOP_FLEET_CRASH_AT"] = crash_at
        if rollback:
            env["FLEET_FAKE_REPAIR"] = "1"
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        validate = f"{sys.executable} {INTEGRATION_VALIDATE}" if rollback else "true"
        command = [sys.executable, "-m", "engine.fleet", "--repo", str(repo), "--name", "parallel",
                   "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
                   "--validate-cmd", validate, "--flag-threshold", "1", "--done-threshold", "1",
                   "--merge-threshold", "1", "--max-parallel", "2", "--round-timeout", "1"]
        crashed = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
        env.pop("LOOP_FLEET_CRASH_AT")
        resumed = (subprocess.run(command + ["--resume"], cwd=ROOT, env=env, text=True,
                                  capture_output=True, timeout=30) if resume else None)
        return temp_dir, temp, repo, crashed, resumed

    def run_fleet(self, *, validate_cmd="true", repair=False):
        temp_dir = tempfile.TemporaryDirectory()
        temp = Path(temp_dir.name)
        repo = temp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "fleet@test.local"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "fleet-test"], cwd=repo, check=True)
        (repo / "goal.md").write_text("Implement both tracks\n", encoding="utf-8")
        subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
        env = dict(os.environ)
        env["LOOP_AGENT_WORKSPACE_ROOT"] = str(temp / "workspace")
        if repair:
            env["FLEET_FAKE_REPAIR"] = "1"
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing if existing else "")
        result = subprocess.run([
            sys.executable, "-m", "engine.fleet", "--repo", str(repo), "--name", "parallel",
            "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
            "--validate-cmd", validate_cmd, "--flag-threshold", "1", "--done-threshold", "1",
            "--merge-threshold", "1", "--max-parallel", "2", "--round-timeout", "1",
        ], cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
        return temp_dir, temp, repo, result

    def test_two_tracks_converge_cas_validate_and_cleanup(self):
        temp_dir, temp, repo, result = self.run_fleet()
        with temp_dir:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            fleet = json.loads((temp / "workspace" / "parallel" / "fleet.json").read_text())
            self.assertEqual(fleet["phase"], "done")
            self.assertEqual([(track["name"], track["status"]) for track in fleet["tracks"]],
                             [("alpha", "cleaned"), ("beta", "cleaned"), ("@final", "cleaned")])
            self.assertEqual((repo / "alpha.txt").read_text(), "alpha\n")
            self.assertEqual((repo / "beta.txt").read_text(), "beta\n")
            self.assertEqual((repo / "final.txt").read_text(), "@final\n")
            self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                            capture_output=True, text=True).stdout, "")
            self.assertEqual(list((temp / "workspace" / "parallel" / "worktrees").iterdir()), [])
            self.assertTrue((temp / "workspace" / "parallel" / "REPORT.md").is_file())
            phases = [entry["phase"] for entry in fleet["phase_history"]]
            for phase in ("planning", "splitting", "exec", "merging", "final", "cleaning", "done"):
                self.assertIn(phase, phases)
            self.assertIsNotNone(fleet["phase_history"][-1]["duration_seconds"])
            self.assertEqual(fleet["initial_integration_sha"],
                             subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=repo,
                                            capture_output=True, text=True, check=True).stdout.strip())
            self.assertTrue(all(track.get("started_at") and track.get("ended_at")
                                for track in fleet["tracks"]))
            merge_stages = {entry["stage"] for entry in fleet["merge_history"]}
            self.assertTrue({"prepared", "ref-updated", "validating", "validated"} <= merge_stages)
            self.assertTrue(all(track["status_history"][0]["status"] == "pending" and
                                track["status_history"][-1]["status"] == "cleaned"
                                for track in fleet["tracks"]))

    def test_imported_parallel_plan_skips_planning_and_consumes_sideband_file(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = temp / "repo"
            repo.mkdir()
            for command in (["git", "init", "-q"],
                            ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                            ["git", "config", "user.email", "fleet@test.local"],
                            ["git", "config", "user.name", "fleet-test"]):
                subprocess.run(command, cwd=repo, check=True)
            (repo / "goal.md").write_text("Imported plan\n", encoding="utf-8")
            subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            plan_path = temp / "plan.json"
            plan_path.write_text(json.dumps([
                {"order": 1, "task": "alpha; DoD: test -f alpha.txt", "track": "alpha"},
                {"order": 2, "task": "beta; DoD: test -f beta.txt", "track": "beta"},
                {"order": 3, "task": "final; DoD: test -f final.txt", "track": "@final"},
            ]), encoding="utf-8")
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(temp / "workspace")}
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            result = subprocess.run([
                sys.executable, "-m", "engine.fleet", "--repo", str(repo), "--name", "parallel",
                "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
                "--validate-cmd", "true", "--done-threshold", "1", "--merge-threshold", "1",
                "--import-plan", str(plan_path), "--consume-import-plan",
            ], cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(plan_path.exists())
            self.assertNotIn("planning handoff", result.stdout)
            self.assertEqual(json.loads(
                (temp / "workspace" / "parallel" / "fleet.json").read_text())["phase"],
                "done")
            self.assertFalse(F.L.pending_runtime_marker_path(
                temp / "workspace", "parallel").exists(),
                "parent coordinator marker durable 後必須清除 root-scoped pending marker")

    def test_split_rejects_occupied_child_namespace_before_any_track_git_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            plan_path = self.write_plan(temp, [
                {"order": 1, "task": "alpha", "track": "alpha", "scope": ["alpha.txt"]},
                {"order": 2, "task": "beta", "track": "beta", "scope": ["beta.txt"]},
            ])
            child = temp / "workspace" / "parallel--alpha"
            child.mkdir(parents=True)
            state = F.L.Workspace.__new__(F.L.Workspace).fresh_state()
            state["config"] = {"repo": "/tmp/unrelated", "agent_cmd": "unrelated-agent",
                               "validate_cmd": "true"}
            encoded = json.dumps(state, ensure_ascii=False, indent=2)
            (child / "state.json").write_text(encoded, encoding="utf-8")
            (child / "state.last-good.json").write_text(encoded, encoding="utf-8")
            marker = child / "must-survive.txt"
            marker.write_text("unrelated workspace\n", encoding="utf-8")

            result = subprocess.run(
                self.fleet_command(repo, import_plan=plan_path), cwd=ROOT,
                env=self.fleet_env(temp), text=True, capture_output=True, timeout=30)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            self.assertIn("child workspace 名稱已被占用", output)
            self.assertEqual(marker.read_text(encoding="utf-8"), "unrelated workspace\n")
            parent = temp / "workspace" / "parallel"
            fleet = json.loads((parent / "fleet.json").read_text(encoding="utf-8"))
            self.assertEqual(fleet["tracks"], [])
            self.assertFalse((parent / "worktrees").exists())
            refs = subprocess.run(
                ["git", "for-each-ref", "--format=%(refname)",
                 f"refs/heads/loop/{fleet['run_id']}/"], cwd=repo,
                text=True, capture_output=True, check=True).stdout.strip()
            self.assertEqual(refs, "")
            self.assertEqual((fleet["phase"], fleet["resume_phase"]),
                             ("failed", "splitting"))

    def test_dashboard_plan_and_config_edits_survive_primary_corruption_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            workspace_root = temp / "workspace"
            command = self.fleet_command(repo) + ["--pause-after-plan"]
            paused = subprocess.run(command, cwd=ROOT, env=self.fleet_env(temp), text=True,
                                    capture_output=True, timeout=30)
            self.assertEqual(paused.returncode, 0, paused.stdout + paused.stderr)
            parent = workspace_root / "parallel"
            fleet_path = parent / "fleet.json"
            fleet = json.loads(fleet_path.read_text(encoding="utf-8"))
            self.assertEqual(fleet["phase"], "awaiting-approval")

            class ResponseCapture:
                response = None

                def _out(self, code, body, _ctype="application/json; charset=utf-8"):
                    self.response = code, json.loads(body)

                def _err(self, message, code=400):
                    self.response = code, {"error": str(message)}

            edited_plan = [dict(task) for task in fleet["plan"]]
            edited_plan[0]["task"] += " (dashboard edit)"
            old_root = D.ROOT
            D.ROOT = workspace_root
            old_load_config = D.load_config
            D.load_config = lambda: {
                "agent_cmds": [{"label": "fake", "cmd": fleet["config"]["agent_cmd"]}],
                "defaults": {}, "extra_path_dirs": [],
            }
            try:
                plan_response = ResponseCapture()
                D.Handler.api_edit_state(plan_response, {
                    "name": "parallel", "run_id": fleet["run_id"], "plan_edit": True,
                    "plan_version": fleet["plan_generation"], "tasks": edited_plan,
                })
                self.assertEqual(plan_response.response[0], 200, plan_response.response)
                config_response = ResponseCapture()
                D.Handler.api_edit_config(config_response, {
                    "name": "parallel", "run_id": fleet["run_id"], "max_parallel": 3,
                })
                self.assertEqual(config_response.response[0], 200, config_response.response)
                primary = json.loads(fleet_path.read_text(encoding="utf-8"))
                checkpoint = json.loads(
                    (parent / "fleet.last-good.json").read_text(encoding="utf-8"))
                self.assertEqual(primary, checkpoint)
                self.assertEqual(primary["config"]["max_parallel"], 3)
                self.assertEqual(primary["plan"][0]["task"], edited_plan[0]["task"])
                fleet_path.write_text("{broken", encoding="utf-8")
                recovered = D.read_parallel_run("parallel")
                self.assertTrue(recovered["fleet_recovery_pending"])
                self.assertEqual(recovered["dashboard_revision"], 2)
            finally:
                D.load_config = old_load_config
                D.ROOT = old_root

            resumed = subprocess.run(command + ["--resume"], cwd=ROOT,
                                     env=self.fleet_env(temp), text=True,
                                     capture_output=True, timeout=40)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            final = json.loads(fleet_path.read_text(encoding="utf-8"))
            self.assertEqual(final["phase"], "done")
            self.assertEqual(final["config"]["max_parallel"], 3)
            self.assertEqual(final["plan"][0]["task"], edited_plan[0]["task"])

    def test_ordinary_at_final_and_reserved_final_have_distinct_identity_and_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            plan = self.write_plan(temp, [
                {"order": 1, "task": "ordinary at-final; DoD: test -f at-final.txt", "track": "at-final"},
                {"order": 2, "task": "beta; DoD: test -f beta.txt", "track": "beta"},
                {"order": 3, "task": "reserved final; DoD: test -f final.txt", "track": "@final"},
            ])
            env = self.fleet_env(temp)
            result = subprocess.run(self.fleet_command(repo, import_plan=plan), cwd=ROOT, env=env,
                                    text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            workspace_root = temp / "workspace"
            parent = workspace_root / "parallel"
            fleet = json.loads((parent / "fleet.json").read_text(encoding="utf-8"))
            self.assertEqual(fleet["phase"], "done")
            tracks = {track["name"]: track for track in fleet["tracks"]}
            ordinary, reserved = tracks["at-final"], tracks["@final"]
            self.assertEqual((ordinary["safe_name"], reserved["safe_name"]), ("at-final", "_final"))
            for field in ("safe_name", "branch_ref", "worktree", "child_workspace", "plan_path"):
                self.assertNotEqual(ordinary[field], reserved[field], field)
            for track in (ordinary, reserved):
                self.assertEqual((track["status"], track["cleanup_stage"]), ("cleaned", "complete"))
                self.assertFalse(Path(track["worktree"]).exists())
                self.assertFalse((workspace_root / track["child_workspace"]).exists())
                self.assertTrue(Path(track["evidence_path"]).is_file())
                self.assertEqual(subprocess.run(
                    ["git", "show-ref", "--verify", track["branch_ref"]], cwd=repo,
                    capture_output=True).returncode, 0)
            self.assertEqual(list((parent / "worktrees").iterdir()), [])
            self.assertEqual((repo / "at-final.txt").read_text(encoding="utf-8"), "at-final\n")
            self.assertEqual((repo / "final.txt").read_text(encoding="utf-8"), "@final\n")

    def test_invalid_numeric_cli_options_fail_before_workspace_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            env = self.fleet_env(temp)
            cases = [
                ("--max-parallel", "0"), ("--max-parallel", "9"),
                ("--merge-threshold", "0"), ("--done-threshold", "0"),
                ("--flag-threshold", "0"), ("--red-limit", "0"),
                ("--stall-limit", "0"), ("--max-child-restarts", "-1"),
                ("--round-timeout", "-0.1"), ("--round-timeout", "nan"),
                ("--validate-timeout", "0"), ("--validate-timeout", "nan"),
                ("--agent-backoff-max", "-1"), ("--agent-backoff-max", "inf"),
            ]
            for index, (option, value) in enumerate(cases):
                with self.subTest(option=option, value=value):
                    name = f"invalid-{index}"
                    result = subprocess.run(
                        self.fleet_command(repo, name=name) + [option, value],
                        cwd=ROOT, env=env, text=True, capture_output=True, timeout=10)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(option, result.stderr)
                    self.assertFalse((temp / "workspace" / name).exists())

    def test_exact_worktree_lock_conflict_leaves_no_parent_ghost(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            lock_path = repo / ".git" / "loop-agent-lite.run.lock"
            lock_path.touch()
            with lock_path.open("r+b") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    self.fleet_command(repo), cwd=ROOT, env=self.fleet_env(temp),
                    text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Git worktree", result.stdout + result.stderr)
            self.assertFalse((temp / "workspace" / "parallel").exists())

    def test_phase_event_sequence_continues_across_bounded_child_truncation(self):
        fleet = F.Fleet.__new__(F.Fleet)
        track = {"name": "alpha", "imported_child_phase_event_seq": 500,
                 "event_history": []}

        def child(first, last):
            return {"phase": "exec", "merge_stage": None, "round": last,
                    "phase_event_seq": last,
                    "phase_events": [
                        {"phase": "exec", "merge_stage": None, "round": seq,
                         "at": f"t-{seq}", "seq": seq}
                        for seq in range(first, last + 1)]}

        self.assertTrue(fleet.sync_child_snapshot(track, child(101, 600)))
        self.assertEqual(track["imported_child_phase_event_seq"], 600)
        self.assertTrue(any(event.get("child_seq") == 600
                            for event in track["event_history"]))
        self.assertTrue(fleet.sync_child_snapshot(track, child(201, 700)))
        self.assertEqual(track["imported_child_phase_event_seq"], 700)
        imported = [event.get("child_seq") for event in track["event_history"]
                    if event.get("event") == "child-phase"]
        self.assertIn(601, imported)
        self.assertIn(700, imported)

    def test_child_adoption_event_is_deduped_by_parent_session(self):
        fleet = F.Fleet.__new__(F.Fleet)
        fleet.state = {"loop": {"session_id": "1" * 32}}
        track = {"event_history": [], "adopted_child_sessions": []}
        child_loop = {"pid": 123, "session_id": "a" * 32}
        self.assertTrue(fleet.record_child_adoption(track, child_loop))
        self.assertFalse(fleet.record_child_adoption(track, child_loop))
        fleet.state["loop"]["session_id"] = "2" * 32
        self.assertTrue(fleet.record_child_adoption(track, child_loop))
        events = [event for event in track["event_history"]
                  if event.get("event") == "child-adopted"]
        self.assertEqual([event["parent_session_id"] for event in events],
                         ["1" * 32, "2" * 32])

    def test_failed_fleet_resumes_legal_phase_and_preserves_last_error_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            marker = temp / "validator-green"
            validator = temp / "transient_validator.py"
            validator.write_text(
                "import pathlib, sys\nsys.exit(0 if pathlib.Path(sys.argv[1]).exists() else 1)\n",
                encoding="utf-8")
            plan = self.write_plan(temp, [
                {"order": 1, "task": "alpha", "track": "alpha"},
                {"order": 2, "task": "beta", "track": "beta"},
            ])
            command = self.fleet_command(
                repo, validate_cmd=f"{sys.executable} {validator} {marker}",
                import_plan=plan) + ["--red-limit", "37", "--stall-limit", "411"]
            failed = subprocess.run(command, cwd=ROOT, env=self.fleet_env(temp),
                                    text=True, capture_output=True, timeout=20)
            self.assertNotEqual(failed.returncode, 0)
            fleet_path = temp / "workspace" / "parallel" / "fleet.json"
            state = json.loads(fleet_path.read_text(encoding="utf-8"))
            self.assertEqual((state["phase"], state["resume_phase"]),
                             ("failed", "splitting"))
            expected_plan, errors = F.validate_plan(json.loads(plan.read_text(encoding="utf-8")))
            self.assertFalse(errors)
            self.assertEqual(state["plan"], expected_plan)
            parent_state = json.loads((fleet_path.parent / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(parent_state["plan"], state["plan"])
            self.assertEqual(state["last_error"]["message"], state["error"])
            self.assertEqual((state["config"]["red_limit"], state["config"]["stall_limit"]),
                             (37, 411))
            marker.touch()
            plan.unlink()
            resume_command = self.fleet_command(
                repo, validate_cmd=f"{sys.executable} {validator} {marker}") + [
                    "--red-limit", "999", "--stall-limit", "999", "--resume"]
            resumed = subprocess.run(resume_command, cwd=ROOT,
                                     env=self.fleet_env(temp), text=True,
                                     capture_output=True, timeout=40)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            final = json.loads(fleet_path.read_text(encoding="utf-8"))
            self.assertEqual(final["phase"], "done")
            self.assertNotIn("error", final)
            self.assertEqual(final["last_error"]["message"], state["error"])
            self.assertEqual((final["config"]["red_limit"], final["config"]["stall_limit"]),
                             (37, 411))
            self.assertNotIn("啟動 planning handoff", resumed.stdout + resumed.stderr)

    def test_cli_resume_uses_frozen_track_env_and_ignores_override(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            plan = self.write_plan(temp, [
                {"order": 1, "task": "alpha", "track": "alpha"},
                {"order": 2, "task": "beta", "track": "beta"},
            ])
            command = self.fleet_command(repo, import_plan=plan)
            env = self.fleet_env(temp, LOOP_FLEET_CRASH_AT="prepared")
            original_env = '{"SERVICE_URL":"http://127.0.0.1:{port}"}'
            crashed = subprocess.run(command + ["--track-env-json", original_env],
                                     cwd=ROOT, env=env, text=True,
                                     capture_output=True, timeout=30)
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            env.pop("LOOP_FLEET_CRASH_AT")
            resumed = subprocess.run(
                command + ["--resume", "--track-env-json", '{"IGNORED":"wrong"}'],
                cwd=ROOT, env=env, text=True, capture_output=True, timeout=40)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            final = json.loads((temp / "workspace" / "parallel" / "fleet.json").read_text())
            self.assertEqual(final["config"]["track_env"],
                             {"SERVICE_URL": "http://127.0.0.1:{port}"})

    def test_primary_candidate_config_wins_over_valid_divergent_checkpoint(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case(
            "track-worktree-created", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            parent = temp / "workspace" / "parallel"
            checkpoint_path = parent / "fleet.last-good.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["config"]["agent_cmd"] = "false"
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            resumed = subprocess.run(
                self.fleet_command(repo) + ["--resume"], cwd=ROOT,
                env=self.fleet_env(temp), text=True, capture_output=True, timeout=40)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            final = json.loads((parent / "fleet.json").read_text(encoding="utf-8"))
            self.assertIn("fleet_fake_agent.py", final["config"]["agent_cmd"])

    def test_integration_worktree_lock_blocks_standalone_during_planning_and_exec(self):
        for fleet_phase in ("planning", "exec"):
            with self.subTest(fleet_phase=fleet_phase), tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                repo = self.make_repo(temp)
                extra_env = {"FLEET_FAKE_PLAN_DELAY": "20"} if fleet_phase == "planning" else {
                    "FLEET_FAKE_EXEC_DELAY": "3"}
                env = self.fleet_env(temp, **extra_env)
                import_plan = None
                if fleet_phase == "exec":
                    import_plan = self.write_plan(temp, [
                        {"order": 1, "task": "alpha", "track": "alpha"},
                        {"order": 2, "task": "beta", "track": "beta"},
                    ])
                fleet_command = self.fleet_command(
                    repo, name=f"fleet-{fleet_phase}", import_plan=import_plan)
                process = subprocess.Popen(fleet_command, cwd=ROOT, env=env, text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    fleet_path = temp / "workspace" / f"fleet-{fleet_phase}" / "fleet.json"
                    if fleet_phase == "planning":
                        self.wait_for_fleet(fleet_path, lambda state: state.get("phase") == "planning")
                    else:
                        self.wait_for_fleet(
                            fleet_path,
                            lambda state: state.get("phase") == "exec" and any(
                                track.get("status") == "running" for track in state.get("tracks", [])))
                    before = (self.git_head(repo), subprocess.run(
                        ["git", "status", "--porcelain"], cwd=repo, text=True,
                        capture_output=True, check=True).stdout)
                    standalone_name = f"standalone-{fleet_phase}"
                    attempt = subprocess.run([
                        sys.executable, "-m", "engine.loop", "--repo", str(repo),
                        "--name", standalone_name, "--agent-cmd", "true",
                        "--validate-cmd", "true", "--preflight-only",
                    ], cwd=ROOT, env=env, text=True, capture_output=True, timeout=10)
                    output = attempt.stdout + attempt.stderr
                    self.assertNotEqual(attempt.returncode, 0, output)
                    self.assertIn(f"Git worktree {repo.resolve()}", output)
                    self.assertIn("單 writer 鎖", output)
                    self.assertFalse((temp / "workspace" / standalone_name / "state.json").exists())
                    after = (self.git_head(repo), subprocess.run(
                        ["git", "status", "--porcelain"], cwd=repo, text=True,
                        capture_output=True, check=True).stdout)
                    self.assertEqual(after, before)
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                    process.communicate(timeout=15)

    def test_rollback_prepared_journal_forces_repair_even_when_validator_now_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            validator = temp / "changing_validator.py"
            validator.write_text(
                "import os\nfrom pathlib import Path\n"
                "root = Path.cwd()\n"
                "candidate = (root / '.git').is_dir() and "
                "((root / 'alpha.txt').exists() or (root / 'beta.txt').exists())\n"
                "if candidate and os.environ.get('ROLLBACK_TEST_FAIL_CANDIDATE') == '1':\n"
                "    raise SystemExit('candidate rejected before crash')\n",
                encoding="utf-8",
            )
            plan = self.write_plan(temp, [
                {"order": 1, "task": "alpha", "track": "alpha"},
                {"order": 2, "task": "beta", "track": "beta"},
            ])
            validate_cmd = f"{sys.executable} {validator}"
            command = self.fleet_command(repo, validate_cmd=validate_cmd, import_plan=plan)
            env = self.fleet_env(
                temp, LOOP_FLEET_CRASH_AT="rollback-prepared", FLEET_FAKE_REPAIR="1",
                ROLLBACK_TEST_FAIL_CANDIDATE="1")
            crashed = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                     capture_output=True, timeout=30)
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            fleet_path = temp / "workspace" / "parallel" / "fleet.json"
            before = json.loads(fleet_path.read_text(encoding="utf-8"))
            self.assertEqual(before["merge_tx"]["stage"], "rollback-prepared")
            self.assertEqual(self.git_head(repo), before["merge_tx"]["candidate_sha"])

            env.pop("LOOP_FLEET_CRASH_AT")
            env.pop("ROLLBACK_TEST_FAIL_CANDIDATE")
            resumed = subprocess.run(command + ["--resume"], cwd=ROOT, env=env, text=True,
                                     capture_output=True, timeout=40)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertIn("resume retry CAS rollback", resumed.stdout)
            self.assertIn("CAS rollback recovery baseline｜validate PASS", resumed.stdout)
            fleet = json.loads(fleet_path.read_text(encoding="utf-8"))
            self.assertEqual(fleet["phase"], "done")
            self.assertIsNone(fleet["merge_tx"])
            repaired = next(track for track in fleet["tracks"]
                            if track["integration_validate_failures"])
            self.assertEqual(repaired["integration_validate_failures"], 1)
            self.assertIn("candidate rejected before crash", repaired["last_integration_error"])
            self.assertTrue(any(event.get("event") == "repairing" and event.get("recovered")
                                for event in repaired["event_history"]))
            self.assertTrue((repo / "compat.txt").is_file())
            self.assertTrue({"rollback-prepared", "rolled-back"} <=
                            {entry["stage"] for entry in fleet["merge_history"]})

    def test_resume_rejects_illegal_recovery_stage_ref_without_touching_git(self):
        for crash_at, illegal_stage in (("prepared", "validating"),
                                        ("ref-updated", "rolled-back")):
            with self.subTest(crash_at=crash_at, illegal_stage=illegal_stage):
                temp_dir, temp, repo, crashed, _ = self.crash_case(crash_at, resume=False)
                with temp_dir:
                    self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
                    workspace = temp / "workspace" / "parallel"
                    original = json.loads((workspace / "fleet.json").read_text(encoding="utf-8"))
                    tx = original["merge_tx"]
                    track = next(item for item in original["tracks"] if item["name"] == tx["track"])
                    before = {
                        "head": self.git_head(repo),
                        "status": subprocess.run(
                            ["git", "status", "--porcelain=v1", "-z"], cwd=repo,
                            text=True, capture_output=True, check=True).stdout,
                        "branch": subprocess.run(
                            ["git", "rev-parse", track["branch_ref"]], cwd=repo,
                            text=True, capture_output=True, check=True).stdout.strip(),
                        "worktree": self.git_head(Path(track["worktree"])),
                    }
                    for filename in ("fleet.json", "fleet.last-good.json"):
                        path = workspace / filename
                        state = json.loads(path.read_text(encoding="utf-8"))
                        state["merge_tx"]["stage"] = illegal_stage
                        path.write_text(json.dumps(state), encoding="utf-8")
                    env = self.fleet_env(temp)
                    resumed = subprocess.run(
                        self.fleet_command(repo) + ["--resume"], cwd=ROOT, env=env,
                        text=True, capture_output=True, timeout=30)
                    output = resumed.stdout + resumed.stderr
                    self.assertNotEqual(resumed.returncode, 0, output)
                    self.assertIn("CAS resume 非法 stage/ref 組合", output)
                    failed = json.loads((workspace / "fleet.json").read_text(encoding="utf-8"))
                    self.assertEqual(failed["phase"], "failed")
                    self.assertIn("CAS resume 非法 stage/ref 組合", failed["error"])
                    after = {
                        "head": self.git_head(repo),
                        "status": subprocess.run(
                            ["git", "status", "--porcelain=v1", "-z"], cwd=repo,
                            text=True, capture_output=True, check=True).stdout,
                        "branch": subprocess.run(
                            ["git", "rev-parse", track["branch_ref"]], cwd=repo,
                            text=True, capture_output=True, check=True).stdout.strip(),
                        "worktree": self.git_head(Path(track["worktree"])),
                    }
                    self.assertEqual(after, before)

    def test_exit_zero_validator_mutation_rolls_back_ref_and_preserves_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            validator = temp / "mutating_validator.py"
            validator.write_text(
                "from pathlib import Path\n"
                "root = Path.cwd()\n"
                "candidate = (root / '.git').is_dir() and "
                "((root / 'alpha.txt').exists() or (root / 'beta.txt').exists())\n"
                "if candidate:\n"
                "    (root / 'validator-preserved.txt').write_text('preserve me\\n')\n",
                encoding="utf-8",
            )
            plan = self.write_plan(temp, [
                {"order": 1, "task": "alpha", "track": "alpha"},
                {"order": 2, "task": "beta", "track": "beta"},
            ])
            result = subprocess.run(
                self.fleet_command(repo, validate_cmd=f"{sys.executable} {validator}",
                                   import_plan=plan),
                cwd=ROOT, env=self.fleet_env(temp), text=True, capture_output=True, timeout=30)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            fleet = json.loads((temp / "workspace" / "parallel" / "fleet.json").read_text(
                encoding="utf-8"))
            self.assertEqual(fleet["phase"], "failed")
            self.assertEqual(fleet["merge_tx"]["stage"], "rollback-prepared")
            self.assertEqual(self.git_head(repo), fleet["merge_tx"]["expected_sha"])
            self.assertEqual((repo / "validator-preserved.txt").read_text(encoding="utf-8"),
                             "preserve me\n")
            self.assertIn("validator-preserved.txt", subprocess.run(
                ["git", "status", "--porcelain"], cwd=repo, text=True,
                capture_output=True, check=True).stdout)
            self.assertIn("validator 修改", fleet["error"])
            self.assertIn("未知 worktree 變更已保留", fleet["error"])
            track = next(item for item in fleet["tracks"]
                         if item["name"] == fleet["merge_tx"]["track"])
            self.assertEqual(subprocess.run(
                ["git", "rev-parse", track["branch_ref"]], cwd=repo, text=True,
                capture_output=True, check=True).stdout.strip(), fleet["merge_tx"]["candidate_sha"])

    def test_cleanup_crash_matrix_resumes_to_cleaned_with_evidence_and_branches(self):
        hooks = ("merged-saved", "cleanup-evidence-captured",
                 "cleanup-worktree-removed", "cleanup-child-removing",
                 "cleanup-child-removed")
        for crash_at in hooks:
            with self.subTest(crash_at=crash_at):
                temp_dir, temp, repo, crashed, resumed = self.crash_case(crash_at)
                with temp_dir:
                    self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
                    self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                    workspace_root = temp / "workspace"
                    parent = workspace_root / "parallel"
                    fleet = json.loads((parent / "fleet.json").read_text(encoding="utf-8"))
                    self.assertEqual(fleet["phase"], "done")
                    self.assertEqual(list((parent / "worktrees").iterdir()), [])
                    for track in fleet["tracks"]:
                        self.assertEqual((track["status"], track["cleanup_stage"]),
                                         ("cleaned", "complete"))
                        evidence_path = Path(track["evidence_path"])
                        evidence = evidence_path.read_bytes()
                        self.assertEqual(hashlib.sha256(evidence).hexdigest(),
                                         track["evidence_sha256"])
                        self.assertFalse((workspace_root / track["child_workspace"]).exists())
                        self.assertFalse((parent / "runtime" / track["safe_name"]).exists())
                        self.assertEqual(subprocess.run(
                            ["git", "show-ref", "--verify", track["branch_ref"]], cwd=repo,
                            capture_output=True).returncode, 0)
                        self.assertEqual(subprocess.run(
                            ["git", "merge-base", "--is-ancestor", track["tip"], "HEAD"],
                            cwd=repo, capture_output=True).returncode, 0)

    def test_cleanup_resumes_partially_unlinked_tombstone_after_state_files_are_gone(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case(
            "cleanup-child-removing", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            parent = temp / "workspace" / "parallel"
            fleet_path = parent / "fleet.json"
            fleet = json.loads(fleet_path.read_text(encoding="utf-8"))
            track = next(item for item in fleet["tracks"]
                         if item.get("cleanup_stage") == "child-removing")
            self.assertRegex(track.get("cleanup_child_generation", ""), r"^[0-9a-f]{32}$")
            self.assertGreater(track.get("cleanup_child_ino", 0), 0)
            child = temp / "workspace" / track["child_workspace"]
            tombstone = Path(track["cleanup_child_tombstone"])
            child.rename(tombstone)
            # Model process death after recursive removal happened to unlink both truth
            # files but before it reached the remaining entries/rmdir.
            for state_name in ("state.json", "state.last-good.json", ".run.lock"):
                (tombstone / state_name).unlink(missing_ok=True)
            (tombstone / "partial-remains").write_text("resume me\n", encoding="utf-8")

            resumed = subprocess.run(
                self.fleet_command(repo) + ["--resume"], cwd=ROOT,
                env=self.fleet_env(temp), text=True, capture_output=True, timeout=30)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            final = json.loads(fleet_path.read_text(encoding="utf-8"))
            self.assertEqual(final["phase"], "done")
            self.assertFalse(tombstone.exists())
            self.assertTrue(all(item["status"] == "cleaned" for item in final["tracks"]))

    def test_resume_rejects_tampered_prompt_evidence_before_cleanup(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case(
            "cleanup-evidence-captured", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            fleet_path = temp / "workspace" / "parallel" / "fleet.json"
            fleet = json.loads(fleet_path.read_text(encoding="utf-8"))
            track = next(item for item in fleet["tracks"]
                         if item.get("cleanup_stage") == "evidence-captured")
            evidence = json.loads(Path(track["evidence_path"]).read_text(encoding="utf-8"))
            self.assertTrue(evidence["prompt_artifacts"])
            prompt = Path(track["evidence_path"]).parent / "prompts" / \
                evidence["prompt_artifacts"][0]["name"]
            prompt.write_text("tampered evidence\n", encoding="utf-8")
            resumed = subprocess.run(
                self.fleet_command(repo) + ["--resume"], cwd=ROOT,
                env=self.fleet_env(temp), text=True, capture_output=True, timeout=30)
            self.assertNotEqual(resumed.returncode, 0)
            self.assertIn("prompt evidence hash/size", resumed.stdout + resumed.stderr)
            self.assertTrue(Path(track["worktree"]).exists())
            self.assertTrue((temp / "workspace" / track["child_workspace"]).exists())

    def test_resume_rejects_evidence_command_hash_even_with_updated_outer_hash(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case(
            "cleanup-evidence-captured", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            parent = temp / "workspace" / "parallel"
            fleet_path = parent / "fleet.json"
            state = json.loads(fleet_path.read_text(encoding="utf-8"))
            track = next(item for item in state["tracks"]
                         if item.get("cleanup_stage") == "evidence-captured")
            evidence_path = Path(track["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(evidence["validate_command_sha256"], hashlib.sha256(
                state["config"]["validate_cmd"].encode()).hexdigest())
            evidence["validate_command_sha256"] = "0" * 64
            evidence_data = json.dumps(evidence, ensure_ascii=False, indent=2).encode()
            evidence_path.write_bytes(evidence_data)
            for state_name in ("fleet.json", "fleet.last-good.json"):
                path = parent / state_name
                candidate = json.loads(path.read_text(encoding="utf-8"))
                candidate_track = next(item for item in candidate["tracks"]
                                       if item["name"] == track["name"])
                candidate_track["evidence_sha256"] = hashlib.sha256(evidence_data).hexdigest()
                path.write_text(json.dumps(candidate), encoding="utf-8")
            resumed = subprocess.run(
                self.fleet_command(repo) + ["--resume"], cwd=ROOT,
                env=self.fleet_env(temp), text=True, capture_output=True, timeout=30)
            self.assertNotEqual(resumed.returncode, 0)
            self.assertIn("evidence command hash", resumed.stdout + resumed.stderr)
            self.assertTrue(Path(track["worktree"]).exists())

    def test_child_removing_never_deletes_unrelated_replacement_workspace(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case(
            "cleanup-child-removing", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            fleet_path = temp / "workspace" / "parallel" / "fleet.json"
            fleet = json.loads(fleet_path.read_text(encoding="utf-8"))
            track = next(item for item in fleet["tracks"]
                         if item.get("cleanup_stage") == "child-removing")
            child = temp / "workspace" / track["child_workspace"]
            preserved = temp / "preserved-original-child"
            child.rename(preserved)
            shutil.copytree(preserved, child)
            for state_name in ("state.json", "state.last-good.json"):
                state_path = child / state_name
                unrelated = json.loads(state_path.read_text(encoding="utf-8"))
                unrelated["fleet_parent_session_id"] = "d" * 32
                state_path.write_text(json.dumps(unrelated), encoding="utf-8")
            marker = child / "must-survive.txt"
            marker.write_text("unrelated\n", encoding="utf-8")
            resumed = subprocess.run(
                self.fleet_command(repo) + ["--resume"], cwd=ROOT,
                env=self.fleet_env(temp), text=True, capture_output=True, timeout=30)
            self.assertNotEqual(resumed.returncode, 0)
            self.assertIn("journal inode 不符",
                          resumed.stdout + resumed.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "unrelated\n")
            self.assertTrue(preserved.exists())

    def test_cleanup_descriptor_rejects_replacement_after_locked_identity(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case(
            "cleanup-child-removing", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            workspace_root = temp / "workspace"
            parent = workspace_root / "parallel"
            state = json.loads((parent / "fleet.json").read_text(encoding="utf-8"))
            track = next(item for item in state["tracks"]
                         if item.get("cleanup_stage") == "child-removing")
            old_root = F.L.WORKSPACE_ROOT
            F.L.WORKSPACE_ROOT = workspace_root
            try:
                args = F.parser().parse_args(self.fleet_command(repo)[3:])
                args.track_env = {}
                fleet = F.Fleet(args)
                fleet.state = state
                fleet.integration_ref = state["integration_ref"]
                fleet.apply_frozen_resume_config()
                child = workspace_root / track["child_workspace"]
                tombstone = Path(track["cleanup_child_tombstone"])
                preserved = temp / "locked-original-child"

                def replace_after_identity(stage, _track, source, _tombstone):
                    self.assertEqual(stage, "after-identity")
                    source.rename(preserved)
                    shutil.copytree(preserved, source)
                    (source / "replacement-marker.txt").write_text(
                        "must survive\n", encoding="utf-8")

                fleet.cleanup_race_hook = replace_after_identity
                with self.assertRaisesRegex(RuntimeError, "驗證後已替換"):
                    fleet.remove_child_workspace(track, child, tombstone)
                self.assertEqual((child / "replacement-marker.txt").read_text(),
                                 "must survive\n")
                self.assertTrue(preserved.exists())
            finally:
                F.L.WORKSPACE_ROOT = old_root

    def test_resume_rejects_symlinked_fleet_truth_and_checkpoint(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case(
            "track-worktree-created", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            parent = temp / "workspace" / "parallel"
            integration_before = self.git_head(repo)
            for name in ("fleet.json", "fleet.last-good.json"):
                path = parent / name
                backing = temp / f"outside-{name}"
                path.rename(backing)
                path.symlink_to(backing)
            resumed = subprocess.run(
                self.fleet_command(repo) + ["--resume"], cwd=ROOT,
                env=self.fleet_env(temp), text=True, capture_output=True, timeout=30)
            output = resumed.stdout + resumed.stderr
            self.assertNotEqual(resumed.returncode, 0, output)
            self.assertIn("symbolic link", output)
            self.assertEqual(self.git_head(repo), integration_before)
            self.assertTrue((parent / "fleet.json").is_symlink())
            self.assertTrue((parent / "fleet.last-good.json").is_symlink())

    def test_immediate_resume_adopts_live_old_session_child_before_restarting(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            plan = self.write_plan(temp, [
                {"order": 1, "task": "alpha", "track": "alpha"},
                {"order": 2, "task": "beta", "track": "beta"},
            ])
            command = self.fleet_command(repo, import_plan=plan, max_parallel=1)
            initial_env = self.fleet_env(temp, FLEET_FAKE_EXEC_DELAY="3")
            supervisor = subprocess.Popen(command, cwd=ROOT, env=initial_env, text=True,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            resumed = None
            old_pid = None
            try:
                fleet_path = temp / "workspace" / "parallel" / "fleet.json"
                fleet = self.wait_for_fleet(
                    fleet_path,
                    lambda state: state.get("phase") == "exec" and any(
                        track.get("status") == "running" for track in state.get("tracks", [])))
                track = next(item for item in fleet["tracks"] if item["status"] == "running")
                child_path = temp / "workspace" / track["child_workspace"]
                deadline = time.monotonic() + 10
                child = {}
                while time.monotonic() < deadline:
                    try:
                        child = json.loads((child_path / "state.json").read_text(encoding="utf-8"))
                        old_pid = child.get("loop", {}).get("pid")
                    except (FileNotFoundError, json.JSONDecodeError):
                        old_pid = None
                    if old_pid and self.file_lock_is_held(child_path / ".run.lock"):
                        break
                    time.sleep(0.05)
                self.assertTrue(old_pid, child)
                old_session = fleet["loop"]["session_id"]
                parent_console = temp / "workspace" / "parallel" / "console.log"
                start_marker = f"啟動 track {track['name']}｜pid="
                self.assertEqual(parent_console.read_text(encoding="utf-8").count(start_marker), 1)

                supervisor.kill()
                supervisor.communicate(timeout=5)
                resumed = subprocess.Popen(command + ["--resume"], cwd=ROOT,
                                           env=self.fleet_env(temp), text=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                adopted = self.wait_for_fleet(
                    fleet_path,
                    lambda state: state.get("loop", {}).get("session_id") not in (None, old_session))
                os.kill(int(old_pid), 0)
                child = json.loads((child_path / "state.json").read_text(encoding="utf-8"))
                self.assertEqual(child["loop"]["pid"], old_pid)
                self.assertNotEqual(child["fleet_parent_session_id"],
                                    adopted["loop"]["session_id"])
                self.assertTrue(self.file_lock_is_held(child_path / ".run.lock"))
                self.assertEqual(parent_console.read_text(encoding="utf-8").count(start_marker), 1,
                                 "resume must adopt the still-live child instead of spawning a duplicate")

                stdout, stderr = resumed.communicate(timeout=40)
                self.assertEqual(resumed.returncode, 0, stdout + stderr)
                final = json.loads(fleet_path.read_text(encoding="utf-8"))
                self.assertEqual(final["phase"], "done")
                final_track = next(item for item in final["tracks"] if item["name"] == track["name"])
                self.assertEqual(final_track["restart_count"], 0)
                self.assertIn(child["loop"]["session_id"],
                              final_track["adopted_child_sessions"])
                self.assertTrue(any(event.get("event") == "child-adopted"
                                    for event in final_track.get("event_history", [])))
                self.assertEqual(parent_console.read_text(encoding="utf-8").count(start_marker), 2,
                                 "replacement may start only after the old lease-bound child exits")
            finally:
                if supervisor.poll() is None:
                    supervisor.kill()
                    supervisor.communicate(timeout=5)
                if resumed is not None and resumed.poll() is None:
                    resumed.send_signal(signal.SIGINT)
                    resumed.communicate(timeout=15)
                if old_pid:
                    try:
                        os.killpg(int(old_pid), signal.SIGINT)
                    except ProcessLookupError:
                        pass

    def test_residual_worktree_unknown_commit_and_symlink_roots_fail_closed(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case("track-worktree-created", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            workspace = temp / "workspace" / "parallel"
            fleet = json.loads((workspace / "fleet.json").read_text(encoding="utf-8"))
            residual = workspace / "worktrees" / "alpha"
            (residual / "human.txt").write_text("preserve unknown commit\n", encoding="utf-8")
            subprocess.run(["git", "add", "human.txt"], cwd=residual, check=True)
            subprocess.run(["git", "commit", "-qm", "human residual commit"], cwd=residual, check=True)
            human_sha = self.git_head(residual)
            integration_before = self.git_head(repo)
            resumed = subprocess.run(
                self.fleet_command(repo) + ["--resume"], cwd=ROOT, env=self.fleet_env(temp),
                text=True, capture_output=True, timeout=30)
            output = resumed.stdout + resumed.stderr
            self.assertNotEqual(resumed.returncode, 0, output)
            self.assertIn("crash 殘留 worktree 已有未知 commit", output)
            branch = f"refs/heads/loop/{fleet['run_id']}/alpha"
            self.assertEqual(self.git_head(residual), human_sha)
            self.assertEqual(subprocess.run(
                ["git", "rev-parse", branch], cwd=repo, text=True,
                capture_output=True, check=True).stdout.strip(), human_sha)
            self.assertEqual((residual / "human.txt").read_text(encoding="utf-8"),
                             "preserve unknown commit\n")
            self.assertEqual(self.git_head(repo), integration_before)
            self.assertFalse((temp / "workspace" / "parallel--alpha").exists())

        for root_name in (".plans", "worktrees", "runtime"):
            with self.subTest(root_name=root_name):
                temp_dir, temp, repo, crashed, _ = self.crash_case(
                    "track-worktree-created", resume=False)
                with temp_dir:
                    self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
                    workspace = temp / "workspace" / "parallel"
                    protected_root = workspace / root_name
                    backing = temp / f"backing-{root_name.lstrip('.')}"
                    protected_root.rename(backing)
                    protected_root.symlink_to(backing, target_is_directory=True)
                    integration_before = self.git_head(repo)
                    resumed = subprocess.run(
                        self.fleet_command(repo) + ["--resume"], cwd=ROOT,
                        env=self.fleet_env(temp), text=True, capture_output=True, timeout=30)
                    output = resumed.stdout + resumed.stderr
                    self.assertNotEqual(resumed.returncode, 0, output)
                    self.assertIn("不可為 symbolic link", output)
                    self.assertTrue(protected_root.is_symlink())
                    self.assertTrue(backing.is_dir())
                    self.assertEqual(self.git_head(repo), integration_before)
                    self.assertFalse((temp / "workspace" / "parallel--alpha").exists())

    def test_cas_success_crash_matrix_resumes_from_ref_truth(self):
        for crash_at in ("track-worktree-created", "prepared", "ref-updated-unjournaled", "ref-updated",
                         "worktree-reset", "validating"):
            with self.subTest(crash_at=crash_at):
                temp_dir, temp, repo, crashed, resumed = self.crash_case(crash_at)
                with temp_dir:
                    self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
                    self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                    fleet = json.loads((temp / "workspace" / "parallel" / "fleet.json").read_text())
                    self.assertEqual(fleet["phase"], "done")
                    self.assertIsNone(fleet["merge_tx"])
                    self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                                                    capture_output=True, text=True).stdout, "")

    def test_merging_interrupt_maps_to_loadable_exec_resume_phase(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case("prepared", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            parent = temp / "workspace" / "parallel"
            fleet_path = parent / "fleet.json"
            state = json.loads(fleet_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "merging")
            F.mark_fleet_interrupted(state)
            self.assertEqual((state["phase"], state["resume_phase"]), ("stopped", "exec"))
            encoded = json.dumps(state, ensure_ascii=False, indent=2)
            (parent / "fleet.json").write_text(encoded, encoding="utf-8")
            (parent / "fleet.last-good.json").write_text(encoded, encoding="utf-8")
            resumed = subprocess.run(
                self.fleet_command(repo) + ["--resume"], cwd=ROOT,
                env=self.fleet_env(temp), text=True, capture_output=True, timeout=40)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            final = json.loads(fleet_path.read_text(encoding="utf-8"))
            self.assertEqual(final["phase"], "done")

    def test_cas_rollback_crash_matrix_resumes_and_repairs(self):
        for crash_at in ("rollback-prepared", "rollback-ref", "rollback-reset", "rolled-back"):
            with self.subTest(crash_at=crash_at):
                temp_dir, temp, repo, crashed, resumed = self.crash_case(crash_at, rollback=True)
                with temp_dir:
                    self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
                    self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                    fleet = json.loads((temp / "workspace" / "parallel" / "fleet.json").read_text())
                    self.assertEqual(fleet["phase"], "done")
                    self.assertIsNone(fleet["merge_tx"])
                    self.assertTrue((repo / "compat.txt").is_file())

    def test_resume_refuses_unknown_third_party_ref_without_resetting_it(self):
        temp_dir, temp, repo, crashed, _unused_resume = self.crash_case("prepared", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            (repo / "human.txt").write_text("preserve me\n", encoding="utf-8")
            subprocess.run(["git", "add", "human.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "human external commit"], cwd=repo, check=True)
            human_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                                       capture_output=True, check=True).stdout.strip()
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(temp / "workspace")}
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            resumed = subprocess.run([
                sys.executable, "-m", "engine.fleet", "--resume", "--repo", str(repo), "--name", "parallel",
                "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
                "--validate-cmd", "true", "--flag-threshold", "1", "--done-threshold", "1",
                "--merge-threshold", "1", "--max-parallel", "2", "--round-timeout", "1",
            ], cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
            self.assertNotEqual(resumed.returncode, 0)
            self.assertIn("第三個 SHA", resumed.stderr)
            self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                                            capture_output=True, check=True).stdout.strip(), human_sha)
            self.assertEqual((repo / "human.txt").read_text(), "preserve me\n")

    def test_rollback_baseline_failure_stops_without_retrying_or_losing_expected_ref(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case("rollback-prepared", rollback=True, resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            fleet_path = temp / "workspace" / "parallel" / "fleet.json"
            before = json.loads(fleet_path.read_text())
            expected = before["merge_tx"]["expected_sha"]
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(temp / "workspace")}
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            env["FLEET_VALIDATE_FORCE_FAIL"] = "1"
            resumed = subprocess.run([
                sys.executable, "-m", "engine.fleet", "--resume", "--repo", str(repo), "--name", "parallel",
                "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
                "--validate-cmd", "true", "--flag-threshold", "1", "--done-threshold", "1",
                "--merge-threshold", "1", "--max-parallel", "2", "--round-timeout", "1",
            ], cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
            self.assertNotEqual(resumed.returncode, 0)
            fleet = json.loads(fleet_path.read_text())
            self.assertEqual(fleet["phase"], "failed")
            self.assertIn("baseline", fleet["error"])
            self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                                            capture_output=True, check=True).stdout.strip(), expected)

    def test_integration_validate_failure_rolls_back_and_agent_repairs(self):
        temp_dir, temp, repo, result = self.run_fleet(
            validate_cmd=f"{sys.executable} {INTEGRATION_VALIDATE}", repair=True)
        with temp_dir:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            fleet = json.loads((temp / "workspace" / "parallel" / "fleet.json").read_text())
            self.assertEqual(fleet["phase"], "done")
            self.assertTrue((repo / "compat.txt").is_file())
            self.assertEqual(sum(track["integration_validate_failures"] for track in fleet["tracks"]), 1)
            repaired = next(track for track in fleet["tracks"] if track["integration_validate_failures"])
            self.assertIn("compat", repaired["last_integration_error"])
            self.assertTrue({"rollback-prepared", "rolled-back"} <=
                            {entry["stage"] for entry in fleet["merge_history"]})
            self.assertIn("last integration error", (temp / "workspace" / "parallel" / "REPORT.md").read_text())
            self.assertIn("已 rollback 並回送 agent 修復", result.stdout)

    def test_interrupt_during_planning_is_resumable_without_stale_parent_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = temp / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "fleet@test.local"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "fleet-test"], cwd=repo, check=True)
            (repo / "goal.md").write_text("Implement both tracks\n", encoding="utf-8")
            subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            env = dict(os.environ)
            env["LOOP_AGENT_WORKSPACE_ROOT"] = str(temp / "workspace")
            env["FLEET_FAKE_PLAN_DELAY"] = "20"
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            command = [
                sys.executable, "-m", "engine.fleet", "--repo", str(repo), "--name", "parallel",
                "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
                "--validate-cmd", "true", "--flag-threshold", "1", "--done-threshold", "1",
                "--merge-threshold", "1", "--max-parallel", "2", "--round-timeout", "1",
            ]
            process = subprocess.Popen(command, cwd=ROOT, env=env, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            fleet_path = temp / "workspace" / "parallel" / "fleet.json"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not fleet_path.exists():
                time.sleep(0.05)
            self.assertTrue(fleet_path.exists(), "planning journal was not created before agent launch")
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout + stderr)
            fleet = json.loads(fleet_path.read_text())
            self.assertEqual((fleet["phase"], fleet["resume_phase"]), ("stopped", "planning"))
            parent = json.loads((temp / "workspace" / "parallel" / "state.json").read_text())
            self.assertIsNone(parent["loop"]["pid"])

            env.pop("FLEET_FAKE_PLAN_DELAY")
            resumed = subprocess.run(command + ["--resume", "--agent-cmd", "false",
                                                "--validate-cmd", "false", "--max-parallel", "1"],
                                     cwd=ROOT, env=env, text=True,
                                     capture_output=True, timeout=30)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertEqual(json.loads(fleet_path.read_text())["phase"], "done")

    def test_external_input_change_during_planning_requests_round_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = self.make_repo(temp)
            env = self.fleet_env(temp, FLEET_FAKE_PLAN_DELAY="2")
            command = self.fleet_command(repo)
            process = subprocess.Popen(command, cwd=ROOT, env=env, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            fleet_path = temp / "workspace" / "parallel" / "fleet.json"
            try:
                self.wait_for_fleet(
                    fleet_path, lambda state: state.get("phase") == "planning", timeout=10)
                parent_path = temp / "workspace" / "parallel" / "state.json"
                deadline = time.monotonic() + 10
                parent = {}
                while time.monotonic() < deadline:
                    try:
                        parent = json.loads(parent_path.read_text(encoding="utf-8"))
                    except (FileNotFoundError, json.JSONDecodeError):
                        time.sleep(0.05)
                        continue
                    if parent.get("round", 0) >= 1 and (parent.get("loop") or {}).get("pid"):
                        break
                    time.sleep(0.05)
                self.assertGreaterEqual(parent.get("round", 0), 1, parent)
                (repo / "goal.md").write_text("Changed requirement\n", encoding="utf-8")
                subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
                subprocess.run(["git", "commit", "-qm", "change frozen input"],
                               cwd=repo, check=True)
                stdout, stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
            self.assertEqual(process.returncode, 0, stdout + stderr)
            fleet = json.loads(fleet_path.read_text(encoding="utf-8"))
            self.assertEqual((fleet["phase"], fleet["resume_phase"]),
                             ("stopped", "planning"))
            self.assertRegex(fleet["stop_reason"], r"input|integration ref")
            parent = json.loads(
                (temp / "workspace" / "parallel" / "state.json").read_text(encoding="utf-8"))
            self.assertIsNone(parent["loop"]["pid"])
            self.assertEqual(parent["round"], 1, "input 變更後不得再啟動下一個 planning round")

    def test_graceful_stop_waits_for_active_child_round_and_resume_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = temp / "repo"
            repo.mkdir()
            for command in (["git", "init", "-q"],
                            ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                            ["git", "config", "user.email", "fleet@test.local"],
                            ["git", "config", "user.name", "fleet-test"]):
                subprocess.run(command, cwd=repo, check=True)
            (repo / "goal.md").write_text("Implement both tracks\n", encoding="utf-8")
            subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            env = dict(os.environ)
            workspace_root = temp / "workspace"
            env["LOOP_AGENT_WORKSPACE_ROOT"] = str(workspace_root)
            env["FLEET_FAKE_EXEC_DELAY"] = "2"
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            command = [sys.executable, "-m", "engine.fleet", "--repo", str(repo), "--name", "parallel",
                       "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
                       "--validate-cmd", "true", "--flag-threshold", "1", "--done-threshold", "1",
                       "--merge-threshold", "1", "--max-parallel", "2", "--round-timeout", "1"]
            process = subprocess.Popen(command, cwd=ROOT, env=env, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            fleet_path = workspace_root / "parallel" / "fleet.json"
            deadline = time.monotonic() + 15
            fleet = {}
            while time.monotonic() < deadline:
                try:
                    fleet = json.loads(fleet_path.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.05)
                    continue
                if fleet.get("phase") == "exec" and any(t.get("status") == "running" for t in fleet["tracks"]):
                    break
                time.sleep(0.05)
            self.assertEqual(fleet.get("phase"), "exec", fleet)
            control = {"schema_version": 1, "run_id": fleet["run_id"], "action": "stop"}
            (workspace_root / "parallel" / "fleet-control.json").write_text(json.dumps(control))
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            fleet = json.loads(fleet_path.read_text())
            self.assertEqual((fleet["phase"], fleet["resume_phase"]), ("stopped", "exec"))
            self.assertTrue(all(track["status"] in {"pending", "stopped", "merge-ready"}
                                for track in fleet["tracks"]))
            for track in fleet["tracks"]:
                child_state = workspace_root / track["child_workspace"] / "state.json"
                if child_state.exists():
                    self.assertIsNone(json.loads(child_state.read_text())["loop"]["pid"])

            env.pop("FLEET_FAKE_EXEC_DELAY")
            resumed = subprocess.run(command + ["--resume"], cwd=ROOT, env=env, text=True,
                                     capture_output=True, timeout=30)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertEqual(json.loads(fleet_path.read_text())["phase"], "done")

    def test_external_goal_commit_gracefully_stops_and_preserves_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = temp / "repo"
            repo.mkdir()
            for command in (["git", "init", "-q"],
                            ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                            ["git", "config", "user.email", "fleet@test.local"],
                            ["git", "config", "user.name", "fleet-test"]):
                subprocess.run(command, cwd=repo, check=True)
            (repo / "goal.md").write_text("Original goal\n", encoding="utf-8")
            subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            workspace_root = temp / "workspace"
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root),
                   "FLEET_FAKE_EXEC_DELAY": "2"}
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            command = [sys.executable, "-m", "engine.fleet", "--repo", str(repo), "--name", "parallel",
                       "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
                       "--validate-cmd", "true", "--flag-threshold", "1", "--done-threshold", "1",
                       "--merge-threshold", "1", "--max-parallel", "2", "--round-timeout", "1"]
            process = subprocess.Popen(command, cwd=ROOT, env=env, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            fleet_path = workspace_root / "parallel" / "fleet.json"
            deadline = time.monotonic() + 15
            fleet = {}
            while time.monotonic() < deadline:
                try:
                    fleet = json.loads(fleet_path.read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.05)
                    continue
                if fleet.get("phase") == "exec" and any(track.get("status") == "running" for track in fleet["tracks"]):
                    break
                time.sleep(0.05)
            self.assertEqual(fleet.get("phase"), "exec", fleet)
            (repo / "goal.md").write_text("Changed human goal\n", encoding="utf-8")
            subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "human changes requirement"], cwd=repo, check=True)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            stopped = json.loads(fleet_path.read_text())
            self.assertEqual((stopped["phase"], stopped["resume_phase"]), ("stopped", "exec"))
            self.assertRegex(stopped["stop_reason"], r"input|integration ref")
            self.assertTrue(all(Path(track["worktree"]).exists() for track in stopped["tracks"]))
            self.assertTrue(all(track["status"] in {"stopped", "merge-ready", "pending"}
                                for track in stopped["tracks"]))

    def test_common_git_dir_writer_lock_blocks_second_fleet_name(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = temp / "repo"
            repo.mkdir()
            for command in (["git", "init", "-q"],
                            ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                            ["git", "config", "user.email", "fleet@test.local"],
                            ["git", "config", "user.name", "fleet-test"]):
                subprocess.run(command, cwd=repo, check=True)
            (repo / "goal.md").write_text("Implement both tracks\n", encoding="utf-8")
            subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(temp / "workspace"),
                   "FLEET_FAKE_PLAN_DELAY": "20"}
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

            def command(name):
                return [sys.executable, "-m", "engine.fleet", "--repo", str(repo), "--name", name,
                        "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
                        "--validate-cmd", "true", "--flag-threshold", "1", "--done-threshold", "1",
                        "--merge-threshold", "1", "--max-parallel", "2", "--round-timeout", "1"]

            first = subprocess.Popen(command("writer-one"), cwd=ROOT, env=env, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                fleet_path = temp / "workspace" / "writer-one" / "fleet.json"
                while time.monotonic() < deadline and not fleet_path.exists():
                    time.sleep(0.05)
                self.assertTrue(fleet_path.exists())
                second = subprocess.run(command("writer-two"), cwd=ROOT, env=env, text=True,
                                        capture_output=True, timeout=10)
                self.assertNotEqual(second.returncode, 0)
                self.assertIn("integration ref refs/heads/main writer", second.stdout + second.stderr)
                self.assertIn("單 writer 鎖", second.stdout + second.stderr)
                self.assertFalse((temp / "workspace" / "writer-two").exists())
            finally:
                first.send_signal(signal.SIGINT)
                first.communicate(timeout=10)

    def test_supervisor_crash_lease_stops_children_after_current_round_then_resume_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = temp / "repo"
            repo.mkdir()
            for command in (["git", "init", "-q"],
                            ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                            ["git", "config", "user.email", "fleet@test.local"],
                            ["git", "config", "user.name", "fleet-test"]):
                subprocess.run(command, cwd=repo, check=True)
            (repo / "goal.md").write_text("Implement both tracks\n", encoding="utf-8")
            subprocess.run(["git", "add", "goal.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
            workspace_root = temp / "workspace"
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root),
                   "FLEET_FAKE_EXEC_DELAY": "2"}
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            command = [sys.executable, "-m", "engine.fleet", "--repo", str(repo), "--name", "parallel",
                       "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
                       "--validate-cmd", "true", "--flag-threshold", "1", "--done-threshold", "1",
                       "--merge-threshold", "1", "--max-parallel", "2", "--round-timeout", "1"]
            process = subprocess.Popen(command, cwd=ROOT, env=env, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            fleet_path = workspace_root / "parallel" / "fleet.json"
            deadline = time.monotonic() + 15
            running = []
            while time.monotonic() < deadline:
                try:
                    fleet = json.loads(fleet_path.read_text())
                    running = [track for track in fleet.get("tracks", [])
                               if track.get("status") == "running"]
                except (FileNotFoundError, json.JSONDecodeError):
                    running = []
                if running:
                    break
                time.sleep(0.05)
            self.assertTrue(running)
            process.kill()
            process.communicate(timeout=5)

            child_paths = [workspace_root / track["child_workspace"] / "state.json" for track in running]
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if all(path.exists() and json.loads(path.read_text()).get("loop", {}).get("pid") is None
                       for path in child_paths):
                    break
                time.sleep(0.1)
            self.assertTrue(all(json.loads(path.read_text())["loop"]["pid"] is None
                                for path in child_paths))
            self.assertTrue(any("lease 已失效" in
                                "\n".join(json.loads(path.read_text()).get("notes", []))
                                for path in child_paths))

            env.pop("FLEET_FAKE_EXEC_DELAY")
            resumed = subprocess.run(command + ["--resume"], cwd=ROOT, env=env, text=True,
                                     capture_output=True, timeout=30)
            self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
            self.assertEqual(json.loads(fleet_path.read_text())["phase"], "done")

    def test_resume_rejects_tampered_plan_hash_in_both_checkpoints(self):
        temp_dir, temp, repo, crashed, _ = self.crash_case("prepared", resume=False)
        with temp_dir:
            self.assertEqual(crashed.returncode, 97, crashed.stdout + crashed.stderr)
            workspace = temp / "workspace" / "parallel"
            for filename in ("fleet.json", "fleet.last-good.json"):
                path = workspace / filename
                state = json.loads(path.read_text())
                state["plan_sha256"] = "0" * 64
                path.write_text(json.dumps(state), encoding="utf-8")
            env = {**os.environ, "LOOP_AGENT_WORKSPACE_ROOT": str(temp / "workspace")}
            env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            resumed = subprocess.run([
                sys.executable, "-m", "engine.fleet", "--resume", "--repo", str(repo), "--name", "parallel",
                "--goal", "goal.md", "--agent-cmd", f"{sys.executable} {FAKE_AGENT}",
                "--validate-cmd", "true", "--flag-threshold", "1", "--done-threshold", "1",
                "--merge-threshold", "1", "--max-parallel", "2", "--round-timeout", "1",
            ], cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
            self.assertNotEqual(resumed.returncode, 0)
            self.assertIn("checkpoint 都不能安全 resume", resumed.stderr)
            self.assertEqual(resumed.stderr.count("fleet master plan hash 不符"), 2)
            self.assertEqual(json.loads((workspace / "fleet.json").read_text())["plan_sha256"], "0" * 64)


if __name__ == "__main__":
    unittest.main()

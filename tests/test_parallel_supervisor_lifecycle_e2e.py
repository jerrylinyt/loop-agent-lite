"""Real active Pause/Resume and Abort lifecycle coverage for parallel runs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from engine import loop as loop_mod
from engine import platform_compat as compat
from engine import repo_owner


REPO_ROOT = Path(__file__).resolve().parent.parent


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("git"), "requires git")
class TestParallelSupervisorLifecycleEndToEnd(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.workspace_root = self.root / "workspaces"
        self.repo.mkdir()
        self.workspace_root.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Parallel Lifecycle E2E")
        git(self.repo, "config", "user.email", "parallel@example.invalid")
        (self.repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
        git(self.repo, "add", "goal.md")
        git(self.repo, "commit", "-qm", "initial")
        self.initial_sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps([
            {"order": 1, "task": "finish lifecycle task", "stack": 1},
        ]), encoding="utf-8")
        self.validator = self.root / "validator.py"
        self.validator.write_text("raise SystemExit(0)\n", encoding="utf-8")
        self.environment = dict(os.environ)
        self.environment.update({
            "LOOP_AGENT_WORKSPACE_ROOT": str(self.workspace_root),
            "PYTHONUTF8": "1",
        })
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup_processes)

    def _parallel_command(self, action: str, *arguments: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "engine.parallel",
            "--workspace-root",
            str(self.workspace_root),
            action,
            *arguments,
        ]

    def _start_command(self, agent: Path) -> list[str]:
        return self._parallel_command(
            "start",
            "--repo", str(self.repo),
            "--name", "base",
            "--import-plan", str(self.plan),
            "--goal", "goal.md",
            "--agent-cmd", compat.join_command([sys.executable, str(agent)]),
            "--validate-cmd", compat.join_command(
                [sys.executable, str(self.validator)]),
            "--done-threshold", "1",
            "--max-parallel", "1",
            "--validate-timeout", "5",
            "--round-timeout", "1",
        )

    def _spawn_start(self, agent: Path) -> subprocess.Popen:
        process = subprocess.Popen(
            self._start_command(agent),
            cwd=REPO_ROOT,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **compat.popen_group_kwargs(),
        )
        self.processes.append(process)
        if compat.attach_process_group(process) is not True:
            process.kill()
            process.wait(timeout=10)
            self.fail("could not contain lifecycle supervisor process")
        return process

    def _cleanup_processes(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                try:
                    compat.kill_process_group(process)
                except (OSError, ProcessLookupError, ValueError):
                    try:
                        process.kill()
                    except OSError:
                        pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            compat.close_process_group(process)

    @staticmethod
    def _diagnostic_file(path: Path, *, limit: int = 64 * 1024) -> str:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return f"<unavailable: {exc}>"
        clipped = raw[-limit:]
        prefix = f"<last {limit} bytes>\n" if len(raw) > limit else ""
        return prefix + clipped.decode("utf-8", errors="replace")

    def _startup_failure_diagnostics(self, state_path: Path) -> str:
        """Preserve durable rc=2 evidence before TemporaryDirectory cleanup."""
        sections = [
            "base state.json:\n" + self._diagnostic_file(state_path),
        ]
        run_id = None
        try:
            state = read_json(state_path)
            parallel = state.get("parallel")
            if isinstance(parallel, dict):
                run_id = parallel.get("run_id")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            sections.append(f"state parse error:\n{exc}")
        if isinstance(run_id, str) and run_id:
            run_dir = self.workspace_root / "base" / "parallel" / run_id
            sections.append(
                "run aggregate.json:\n"
                + self._diagnostic_file(run_dir / "aggregate.json"))
        console_paths = sorted(self.workspace_root.rglob("console.log"))[:8]
        if console_paths:
            sections.extend(
                f"console {path}:\n{self._diagnostic_file(path)}"
                for path in console_paths)
        else:
            sections.append("console logs:\n<none>")
        try:
            marker = repo_owner.RepoOwnerFence.inspect(self.repo)
            owner = json.dumps(marker, ensure_ascii=False, indent=2)
        except (OSError, ValueError, repo_owner.RepoOwnerError) as exc:
            owner = f"<unavailable: {exc}>"
        sections.append("repo owner marker:\n" + owner)
        return "\n\n".join(sections)

    def _wait_for_active_agent(
        self, process: subprocess.Popen, *, timeout: float = 30,
    ) -> tuple[dict, Path]:
        state_path = self.workspace_root / "base" / "state.json"
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                diagnostics = self._startup_failure_diagnostics(state_path)
                self.fail(
                    f"parallel supervisor exited before worker became active "
                    f"(rc={process.returncode}):\n{output}\n\n{diagnostics}")
            try:
                state = read_json(state_path)
                run_id = state["parallel"]["run_id"]
                marker = (self.workspace_root
                          / f"base--{run_id}-task-1" / "agent-active.json")
                if marker.is_file():
                    return state, marker
            except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
                last_error = exc
            time.sleep(0.02)
        self.fail(f"active worker marker was not published: {last_error}")

    def _control(self, action: str, *, timeout: float = 120) -> subprocess.CompletedProcess:
        result = subprocess.run(
            self._parallel_command(action, "base"),
            cwd=REPO_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        state_path = self.workspace_root / "base" / "state.json"
        try:
            state_diagnostic = state_path.read_text(encoding="utf-8")
        except OSError as exc:
            state_diagnostic = f"<unavailable: {exc}>"
        self.assertEqual(
            result.returncode, 0,
            f"parallel {action} failed\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}\nstate:\n{state_diagnostic}",
        )
        return result

    def _finish_start(
        self, process: subprocess.Popen, *, expected: int = 0,
        timeout: float = 30,
    ) -> str:
        try:
            output, _unused = process.communicate(timeout=timeout)
        finally:
            if process.poll() is not None:
                compat.close_process_group(process)
        self.assertEqual(
            process.returncode, expected,
            f"parallel start exited {process.returncode}:\n{output}",
        )
        return output

    def test_active_pause_then_new_supervisor_resume_completes(self):
        agent = self.root / "pause-resume-agent.py"
        agent.write_text(
            """\
import os
import pathlib
import subprocess
import sys
import time

workspace = pathlib.Path(os.environ["LOOP_WS"])
counter = workspace / "agent-invocations.txt"
try:
    invocation = int(counter.read_text(encoding="utf-8")) + 1
except FileNotFoundError:
    invocation = 1
counter.write_text(str(invocation), encoding="utf-8")
(workspace / "agent-active.json").write_text(str(invocation), encoding="utf-8")
if invocation == 1:
    time.sleep(2)
    raise SystemExit(0)

branch = subprocess.run(
    ["git", "symbolic-ref", "--short", "HEAD"], check=True,
    capture_output=True, text=True).stdout.strip()
run_id = branch.split("/")[-2]
sync_ref = f"refs/heads/loop/{run_id}/integration"
subprocess.run(["git", "merge", "--no-edit", sync_ref], check=True,
               capture_output=True, text=True)
pathlib.Path("lifecycle-completed.txt").write_text("completed\\n", encoding="utf-8")
subprocess.run(["git", "add", "lifecycle-completed.txt"], check=True)
if subprocess.run(["git", "status", "--porcelain"], check=True,
                  capture_output=True, text=True).stdout.strip():
    subprocess.run(["git", "commit", "-qm", "complete after resume"], check=True)
subprocess.run([sys.executable, "-m", "engine.work", "done", "task-1"],
               check=True, env=os.environ.copy())
""",
            encoding="utf-8",
        )

        starter = self._spawn_start(agent)
        active_state, _marker = self._wait_for_active_agent(starter)
        run_id = active_state["parallel"]["run_id"]
        self._control("pause")
        self._finish_start(starter)

        paused = read_json(self.workspace_root / "base" / "state.json")
        self.assertEqual(paused["parallel"]["status"], "paused")
        self.assertIsNone(paused["loop"]["pid"])
        self.assertEqual(
            paused["parallel"]["tasks"][0]["resource_state"], "paused")
        self.assertIsNone(paused["parallel"]["terminal_intent"])
        worktree = (self.workspace_root / "base" / "worktrees"
                    / f"{run_id}-task-1")
        self.assertTrue(worktree.is_dir())
        run_dir = self.workspace_root / "base" / "parallel" / run_id
        assignment = read_json(run_dir / "assignments" / "task-1.json")
        worker_state = read_json(
            Path(assignment["worker_workspace_path"]) / "state.json")
        self.assertEqual(worker_state["assignment"]["status"], "paused")
        self.assertEqual(worker_state["assignment"]["pause_generation"], 1)

        self._control("resume")

        completed = read_json(self.workspace_root / "base" / "state.json")
        self.assertEqual(completed["parallel"]["status"], "completed")
        self.assertEqual(completed["phase"], "done")
        self.assertIsNone(completed["loop"]["pid"])
        self.assertEqual(completed["completed"][0]["order"], 1)
        self.assertEqual(
            (self.repo / "lifecycle-completed.txt").read_text(encoding="utf-8"),
            "completed\n",
        )
        self.assertTrue((run_dir / "receipts" / "task-1.json").is_file())
        self.assertFalse(worktree.exists())
        worker_workspace = Path(assignment["worker_workspace_path"])
        worker_archive = run_dir / "worker-archives" / "task-1"
        self.assertFalse(worker_workspace.exists())
        self.assertTrue((worker_archive / "state.json").is_file())

        # Simulate the terminal two-file checkpoint crash: aggregate and
        # SHUTDOWN are durable, while the base projection still says
        # finalizing.  Resume must repair only that projection.
        aggregate_before = (run_dir / "aggregate.json").read_bytes()
        lagging = dict(completed)
        lagging["parallel"] = dict(lagging["parallel"])
        lagging["parallel"]["status"] = "finalizing"
        lagging["loop"] = dict(lagging["loop"])
        lagging["loop"]["pid"] = 999999
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        self._control("resume")
        repaired = read_json(self.workspace_root / "base" / "state.json")
        self.assertEqual(repaired["parallel"]["status"], "completed")
        self.assertIsNone(repaired["loop"]["pid"])
        self.assertEqual(
            (run_dir / "aggregate.json").read_bytes(), aggregate_before)

        # Missing finalization evidence must block projection repair, but a
        # failed generation claim must not make the same repaired evidence
        # unrecoverable on the next attempt.
        finalization_path = run_dir / "finalization.json"
        finalization_bytes = finalization_path.read_bytes()
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        finalization_path.unlink()
        missing_outbox = subprocess.run(
            self._parallel_command("resume", "base"), cwd=REPO_ROOT,
            env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(missing_outbox.returncode, 0)
        self.assertIn("finalization", missing_outbox.stderr)
        finalization_path.write_bytes(finalization_bytes)
        self._control("resume")

        # Removing the guardian evidence tree cannot be interpreted as an
        # empty/safely reaped worker set merely because the archived workspace
        # and Git cleanup look complete.
        children_root = run_dir / "children"
        hidden_children = run_dir / "children.hidden-for-test"
        os.replace(children_root, hidden_children)
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        missing_children = subprocess.run(
            self._parallel_command("resume", "base"), cwd=REPO_ROOT,
            env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(missing_children.returncode, 0)
        self.assertIn("payload evidence", missing_children.stderr)
        os.replace(hidden_children, children_root)
        self._control("resume")

        # An empty real directory cannot replace the archived integrated
        # checkpoint.  The archive must retain a worker state bound to the
        # immutable assignment and canonical receipt.
        hidden_archive = run_dir / "worker-archives" / "task-1.hidden-for-test"
        os.replace(worker_archive, hidden_archive)
        worker_archive.mkdir()
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        empty_archive = subprocess.run(
            self._parallel_command("resume", "base"), cwd=REPO_ROOT,
            env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(empty_archive.returncode, 0)
        self.assertIn("worker checkpoint", empty_archive.stderr)
        worker_archive.rmdir()
        os.replace(hidden_archive, worker_archive)
        self._control("resume")

        # A durable integrated worker checkpoint can only exist after launch
        # authorization was published.  Removing every authorized response is
        # evidence loss, even when receipts and child records remain intact.
        launch_responses = run_dir / "launches" / "responses"
        hidden_launch_responses = run_dir / "launch-responses.hidden-for-test"
        hidden_launch_responses.mkdir()
        response_paths = list(launch_responses.glob("*.json"))
        self.assertTrue(response_paths)
        for response_path in response_paths:
            os.replace(response_path, hidden_launch_responses / response_path.name)
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        missing_launch_response = subprocess.run(
            self._parallel_command("resume", "base"), cwd=REPO_ROOT,
            env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(missing_launch_response.returncode, 0)
        self.assertIn("authorized launch response",
                      missing_launch_response.stderr)
        for response_path in hidden_launch_responses.glob("*.json"):
            os.replace(response_path, launch_responses / response_path.name)
        hidden_launch_responses.rmdir()
        self._control("resume")

        # Hash-looking identity fields are not enough: the archived checkpoint
        # must contain the exact frozen plan/task text.
        archived_state_path = worker_archive / "state.json"
        archived_state_bytes = archived_state_path.read_bytes()
        archived_state = json.loads(archived_state_bytes.decode("utf-8"))
        archived_state["plan"][0]["task"] = "tampered archived task"
        loop_mod.write_checkpointed_state(
            archived_state_path,
            json.dumps(archived_state, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        tampered_worker_plan = subprocess.run(
            self._parallel_command("resume", "base"), cwd=REPO_ROOT,
            env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(tampered_worker_plan.returncode, 0)
        self.assertIn("checkpoint plan mismatch", tampered_worker_plan.stderr)
        loop_mod.write_checkpointed_state(
            archived_state_path, archived_state_bytes)
        self._control("resume")

        # A leftover task ref is a terminal Git resource even when the path and
        # worktree registry are absent.
        task_ref = f"refs/heads/loop/{run_id}/task-1"
        git(self.repo, "update-ref", task_ref, "HEAD")
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        leftover_ref = subprocess.run(
            self._parallel_command("resume", "base"), cwd=REPO_ROOT,
            env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(leftover_ref.returncode, 0)
        self.assertIn("worktree/ref resources", leftover_ref.stderr)
        git(self.repo, "update-ref", "-d", task_ref)
        self._control("resume")

        # The same projection repair must fail closed if repository truth was
        # changed after terminalization.  A terminal aggregate is not authority
        # to overwrite or bless a different primary/sync tip.
        git(self.repo, "commit", "--allow-empty", "-qm", "tamper terminal primary")
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        rejected = subprocess.run(
            self._parallel_command("resume", "base"),
            cwd=REPO_ROOT,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("terminal projection", rejected.stderr)
        still_lagging = read_json(self.workspace_root / "base" / "state.json")
        self.assertEqual(still_lagging["parallel"]["status"], "finalizing")
        self.assertEqual(
            (run_dir / "aggregate.json").read_bytes(), aggregate_before)

    def test_active_abort_cancels_and_cleans_without_receipt(self):
        agent = self.root / "abort-agent.py"
        agent.write_text(
            """\
import os
import pathlib
import time

workspace = pathlib.Path(os.environ["LOOP_WS"])
(workspace / "agent-active.json").write_text("active", encoding="utf-8")
time.sleep(2)
""",
            encoding="utf-8",
        )

        starter = self._spawn_start(agent)
        active_state, _marker = self._wait_for_active_agent(starter)
        run_id = active_state["parallel"]["run_id"]
        self._control("abort")
        self._finish_start(starter)

        cancelled = read_json(self.workspace_root / "base" / "state.json")
        self.assertEqual(cancelled["parallel"]["status"], "cancelled")
        self.assertEqual(cancelled["parallel"]["terminal_intent"], "cancelled")
        self.assertEqual(cancelled["phase"], "exec")
        self.assertIsNone(cancelled["loop"]["pid"])
        task = cancelled["parallel"]["tasks"][0]
        self.assertEqual(task["outcome"], "cancelled")
        self.assertEqual(task["resource_state"], "cleaned")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.initial_sha)
        run_dir = self.workspace_root / "base" / "parallel" / run_id
        assignment = read_json(run_dir / "assignments" / "task-1.json")
        self.assertFalse((run_dir / "receipts" / "task-1.json").exists())
        self.assertFalse(
            (self.workspace_root / "base" / "worktrees"
             / f"{run_id}-task-1").exists())
        self.assertFalse(Path(assignment["worker_workspace_path"]).exists())
        self.assertTrue(
            (run_dir / "worker-archives" / "task-1" / "state.json").is_file())
        self.assertTrue((self.workspace_root / "base" / "REPORT.md").is_file())

        # Claimed launch authority is independent evidence that the worker ran.
        # Losing both child records and the archive must not be reclassified as
        # a never-dispatched future task.
        children_root = run_dir / "children"
        hidden_children = run_dir / "children.hidden-for-test"
        archive = run_dir / "worker-archives" / "task-1"
        hidden_archive = run_dir / "worker-archives" / "task-1.hidden-for-test"
        os.replace(children_root, hidden_children)
        os.replace(archive, hidden_archive)
        lagging = dict(cancelled)
        lagging["parallel"] = dict(lagging["parallel"])
        lagging["parallel"]["status"] = "finalizing_cancel"
        lagging["loop"] = dict(lagging["loop"])
        lagging["loop"]["pid"] = 999999
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        missing_launch_evidence = subprocess.run(
            self._parallel_command("resume", "base"), cwd=REPO_ROOT,
            env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(missing_launch_evidence.returncode, 0)
        self.assertIn("claimed launch lacks reaped payload evidence",
                      missing_launch_evidence.stderr)
        os.replace(hidden_children, children_root)
        os.replace(hidden_archive, archive)
        self._control("resume")

        # A cancelled archive may reflect an interrupted in-flight status, but
        # any checkpoint that is present must still be a valid managed-worker
        # state bound to this exact run and assignment.
        archived_state_path = archive / "state.json"
        archived_state_bytes = archived_state_path.read_bytes()
        loop_mod.write_checkpointed_state(archived_state_path, b"{}")
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        malformed_cancelled_checkpoint = subprocess.run(
            self._parallel_command("resume", "base"), cwd=REPO_ROOT,
            env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(malformed_cancelled_checkpoint.returncode, 0)
        self.assertIn("worker checkpoint",
                      malformed_cancelled_checkpoint.stderr)
        loop_mod.write_checkpointed_state(
            archived_state_path, archived_state_bytes)
        self._control("resume")

        # The archive authority records whether a recoverable checkpoint was
        # present at the atomic move.  Deleting both copies later is therefore
        # detectable instead of being mistaken for an originally empty worker.
        archived_checkpoint_path = loop_mod.state_checkpoint_path(
            archived_state_path)
        archived_state_path.unlink()
        archived_checkpoint_path.unlink()
        loop_mod.write_checkpointed_state(
            self.workspace_root / "base" / "state.json",
            json.dumps(lagging, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        deleted_cancelled_checkpoint = subprocess.run(
            self._parallel_command("resume", "base"), cwd=REPO_ROOT,
            env=self.environment, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(deleted_cancelled_checkpoint.returncode, 0)
        self.assertIn("archive authority/checkpoint mismatch",
                      deleted_cancelled_checkpoint.stderr)
        loop_mod.write_checkpointed_state(
            archived_state_path, archived_state_bytes)
        self._control("resume")


if __name__ == "__main__":
    unittest.main()

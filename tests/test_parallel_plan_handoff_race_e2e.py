"""True cross-process Dashboard plan handoff race against the base run lock."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from engine import parallel_state
from engine import platform_compat as compat


REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = Path(__file__).with_name("parallel_plan_handoff_race_harness.py")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@unittest.skipUnless(shutil.which("git"), "requires git")
class TestParallelPlanHandoffRaceEndToEnd(unittest.TestCase):
    def test_two_dashboard_handoffs_cannot_exchange_or_overwrite_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = root / "repo"
            workspace_root = root / "workspaces"
            repo.mkdir()
            workspace_root.mkdir()
            _git(repo, "init", "-q")
            _git(repo, "config", "user.name", "Plan Handoff Race")
            _git(repo, "config", "user.email", "handoff@example.invalid")
            (repo / "goal.md").write_text("# Goal\n", encoding="utf-8")
            _git(repo, "add", "goal.md")
            _git(repo, "commit", "-qm", "initial")

            validator = root / "validator.py"
            validator.write_text("raise SystemExit(0)\n", encoding="utf-8")
            agent = root / "agent.py"
            agent.write_text(
                """\
import os
import pathlib
import subprocess
import sys
import time

label, ready_name, finish_name = sys.argv[1:4]
ready = pathlib.Path(ready_name)
finish = pathlib.Path(finish_name)
ready.write_text(label + "\\n", encoding="utf-8")
deadline = time.monotonic() + 60
while not finish.is_file():
    if time.monotonic() >= deadline:
        raise SystemExit("agent finish barrier timed out")
    time.sleep(0.01)
branch = subprocess.run(
    ["git", "symbolic-ref", "--short", "HEAD"], check=True,
    capture_output=True, text=True).stdout.strip()
parts = branch.split("/")
run_id = parts[-2]
order = int(parts[-1].removeprefix("task-"))
sync_ref = f"refs/heads/loop/{run_id}/integration"
subprocess.run(["git", "merge", "--no-edit", sync_ref], check=True,
               capture_output=True, text=True)
target = pathlib.Path(f"race-{label}.txt")
if not target.exists():
    target.write_text(label + "\\n", encoding="utf-8")
subprocess.run(["git", "add", str(target)], check=True)
if subprocess.run(
        ["git", "status", "--porcelain"], check=True,
        capture_output=True, text=True).stdout.strip():
    subprocess.run(
        ["git", "commit", "-qm", f"race winner {label}"], check=True)
subprocess.run([sys.executable, "-m", "engine.work", "done", f"task-{order}"],
               check=True, env=os.environ.copy())
""",
                encoding="utf-8",
            )

            go = root / "supervisor-go"
            labels = ("alpha", "beta")
            tasks = ("immutable plan alpha", "immutable plan beta")
            ready_paths = [root / f"ready-{label}.json" for label in labels]
            agent_ready = [root / f"agent-{label}.ready" for label in labels]
            agent_finish = [root / f"agent-{label}.finish" for label in labels]
            environment = dict(os.environ)
            old_pythonpath = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                str(REPO_ROOT) if not old_pythonpath
                else str(REPO_ROOT) + os.pathsep + old_pythonpath)
            environment.update({
                "LOOP_AGENT_WORKSPACE_ROOT": str(workspace_root),
                "PYTHONUTF8": "1",
            })
            processes: list[subprocess.Popen] = []
            captured: dict[int, tuple[str, str]] = {}

            def command(index: int) -> list[str]:
                return [
                    sys.executable, str(HARNESS),
                    "--repo", str(repo),
                    "--workspace-root", str(workspace_root),
                    "--name", "base",
                    "--label", labels[index],
                    "--task", tasks[index],
                    "--ready", str(ready_paths[index]),
                    "--go", str(go),
                    "--agent", str(agent),
                    "--validator", str(validator),
                    "--agent-ready", str(agent_ready[index]),
                    "--agent-finish", str(agent_finish[index]),
                    "--barrier-timeout", "30",
                ]

            def spawn(index: int) -> subprocess.Popen:
                process = subprocess.Popen(
                    command(index), cwd=REPO_ROOT, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    **compat.popen_group_kwargs())
                processes.append(process)
                if compat.attach_process_group(process) is not True:
                    process.kill()
                    process.wait(timeout=10)
                    self.fail(f"could not contain handoff contender {labels[index]}")
                return process

            def finish_process(
                    process: subprocess.Popen, timeout: float) -> tuple[str, str]:
                key = id(process)
                if key not in captured:
                    captured[key] = process.communicate(timeout=timeout)
                    compat.close_process_group(process)
                return captured[key]

            def wait_ready(
                    process: subprocess.Popen, path: Path,
                    *, timeout: float = 20.0) -> dict:
                deadline = time.monotonic() + timeout
                last_error = None
                while time.monotonic() < deadline:
                    try:
                        return _read_json(path)
                    except (FileNotFoundError, json.JSONDecodeError) as exc:
                        last_error = exc
                    if process.poll() is not None:
                        stdout, stderr = finish_process(process, 5)
                        self.fail(
                            f"contender exited before ready rc={process.returncode}\n"
                            f"stdout:\n{stdout}\nstderr:\n{stderr}")
                    time.sleep(0.01)
                self.fail(f"contender ready timeout {path}: {last_error}")

            try:
                # Stage under two independent processes/owner generations, but
                # hold both at the same go barrier before the real CLI race.
                first = spawn(0)
                first_ready = wait_ready(first, ready_paths[0])
                second = spawn(1)
                second_ready = wait_ready(second, ready_paths[1])
                ready = [first_ready, second_ready]

                staged_paths = [Path(item["staged_path"]) for item in ready]
                self.assertNotEqual(staged_paths[0], staged_paths[1])
                for item, staged_path in zip(ready, staged_paths):
                    raw = staged_path.read_bytes()
                    self.assertEqual(
                        hashlib.sha256(raw).hexdigest(),
                        item["staged_sha256"],
                    )
                    argv = item["argv"]
                    self.assertEqual(
                        argv[argv.index("--import-plan") + 1], str(staged_path))
                    self.assertEqual(
                        argv[argv.index("--expected-plan-sha256") + 1],
                        item["staged_sha256"],
                    )

                go.write_bytes(b"go\n")
                deadline = time.monotonic() + 45
                winner_index = None
                while time.monotonic() < deadline:
                    published = [path.is_file() for path in agent_ready]
                    if sum(published) == 1:
                        winner_index = published.index(True)
                        break
                    if sum(published) > 1:
                        self.fail("both supervisors released a worker payload")
                    if all(process.poll() is not None for process in processes):
                        diagnostics = []
                        for process in processes:
                            stdout, stderr = finish_process(process, 5)
                            diagnostics.append(
                                f"rc={process.returncode}\nstdout={stdout}\nstderr={stderr}")
                        self.fail("both contenders exited before agent ready:\n"
                                  + "\n---\n".join(diagnostics))
                    time.sleep(0.02)
                self.assertIsNotNone(winner_index, "winner agent did not become ready")
                assert winner_index is not None
                loser_index = 1 - winner_index
                time.sleep(0.2)
                self.assertFalse(
                    agent_ready[loser_index].exists(),
                    "losing supervisor released its different plan")

                base = workspace_root / "base"
                run_dirs = sorted(
                    path for path in (base / "parallel").iterdir()
                    if path.is_dir())
                self.assertEqual(len(run_dirs), 1)
                run_dir = run_dirs[0]
                run_id = run_dir.name
                durable_plan = _read_json(run_dir / "plan.json")
                manifest = _read_json(run_dir / "manifest.json")
                self.assertEqual(durable_plan[0]["task"], tasks[winner_index])
                self.assertNotEqual(durable_plan[0]["task"], tasks[loser_index])
                self.assertEqual(manifest["run_id"], run_id)
                self.assertEqual(
                    manifest["plan_hash"],
                    parallel_state.canonical_json_hash(durable_plan),
                )
                winning_raw = staged_paths[winner_index].read_bytes()
                self.assertEqual(
                    hashlib.sha256(winning_raw).hexdigest(),
                    ready[winner_index]["staged_sha256"],
                )
                winning_argv = ready[winner_index]["argv"]
                self.assertEqual(
                    winning_argv[
                        winning_argv.index("--expected-plan-sha256") + 1],
                    ready[winner_index]["staged_sha256"],
                )
                state = _read_json(base / "state.json")
                self.assertEqual(state["parallel"]["run_id"], run_id)

                refs = [line for line in _git(
                    repo, "for-each-ref", "--format=%(refname)",
                    "refs/heads/loop/").stdout.splitlines() if line]
                self.assertIn(manifest["integration_ref"], refs)
                self.assertIn(f"refs/heads/loop/{run_id}/task-1", refs)
                self.assertTrue(all(
                    ref.startswith(f"refs/heads/loop/{run_id}/")
                    for ref in refs))

                loser = processes[loser_index]
                loser_stdout, loser_stderr = finish_process(loser, 15)
                self.assertNotEqual(
                    loser.returncode, 0,
                    f"loser unexpectedly succeeded\nstdout:\n{loser_stdout}\n"
                    f"stderr:\n{loser_stderr}",
                )
                self.assertIsNone(processes[winner_index].poll())

                agent_finish[winner_index].write_bytes(b"finish\n")
                winner = processes[winner_index]
                winner_stdout, winner_stderr = finish_process(winner, 60)
                self.assertEqual(
                    winner.returncode, 0,
                    f"winner failed\nstdout:\n{winner_stdout}\n"
                    f"stderr:\n{winner_stderr}",
                )
                self.assertEqual(
                    sum(process.returncode == 0 for process in processes), 1)
                self.assertEqual(len([
                    path for path in (base / "parallel").iterdir()
                    if path.is_dir()
                ]), 1)
                self.assertEqual(
                    _read_json(run_dir / "plan.json"), durable_plan)
                self.assertEqual(
                    (repo / f"race-{labels[winner_index]}.txt").read_text(
                        encoding="utf-8"),
                    labels[winner_index] + "\n",
                )
                self.assertFalse(
                    (repo / f"race-{labels[loser_index]}.txt").exists())
                for item, staged_path in zip(ready, staged_paths):
                    self.assertEqual(
                        hashlib.sha256(staged_path.read_bytes()).hexdigest(),
                        item["staged_sha256"],
                    )
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        try:
                            compat.kill_process_group(process)
                        except (OSError, ProcessLookupError, ValueError):
                            try:
                                process.kill()
                            except OSError:
                                pass
                    try:
                        if id(process) not in captured:
                            process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=10)
                    compat.close_process_group(process)


if __name__ == "__main__":
    unittest.main()

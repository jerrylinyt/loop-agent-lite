"""Containment and side-effect guards for ordinary-runner Git reads."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from engine import loop as loop_mod
from engine import parallel
from engine import platform_compat as compat
from engine import ralph as ralph_mod
from engine import repo_owner


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True,
        capture_output=True, text=True,
    )


@unittest.skipUnless(shutil.which("git"), "git is required")
class GitReadFenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.name", "Git Read Fence Test")
        _git(self.repo, "config", "user.email", "git-read@example.invalid")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-qm", "initial")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state_path = self.workspace / "state.json"
        self.state_path.write_text("{}\n", encoding="utf-8")
        self._old_owner = loop_mod._REPO_OWNER_FENCE
        self.addCleanup(self._restore_owner_global)

    def _restore_owner_global(self):
        fence = loop_mod._REPO_OWNER_FENCE
        if fence is not None and fence is not self._old_owner:
            try:
                marker = fence.marker
                if marker["state"] in {"active", "recovering"} and marker[
                        "child_state"] in {"idle", "child_reaped"}:
                    fence.terminalize("git-read-test-finished")
                else:
                    fence.close()
            except (OSError, repo_owner.RepoOwnerError):
                fence.close()
        loop_mod._REPO_OWNER_FENCE = self._old_owner

    def _claim(self, kind=repo_owner.OwnerKind.LOOP):
        self.assertIsNone(loop_mod._REPO_OWNER_FENCE)
        fence = repo_owner.RepoOwnerFence.claim(
            self.repo, owner_kind=kind, workspace=self.workspace,
            state_path=self.state_path,
        )
        loop_mod._REPO_OWNER_FENCE = fence
        return fence

    def _external_helper(self) -> tuple[Path, Path]:
        called = self.root / "external-helper-called"
        if compat.IS_WINDOWS:
            helper = self.root / "slow-helper.cmd"
            helper.write_text(
                "@echo off\r\n"
                f">\"{called}\" echo called\r\n"
                "ping -n 20 127.0.0.1 >nul\r\n",
                encoding="utf-8",
            )
        else:
            helper = self.root / "slow-helper"
            helper.write_text(
                "#!/bin/sh\n"
                f": > {str(called)!r}\n"
                "sleep 20\n",
                encoding="utf-8",
            )
            helper.chmod(0o755)
        return helper, called

    def test_read_argv_is_hardened_and_always_requests_git_child(self):
        completed = subprocess.CompletedProcess([], 0, "clean\n", "")
        with mock.patch.object(loop_mod, "sh", return_value=completed) as run:
            result = loop_mod.git(
                self.repo, "status", "--porcelain=v1", check=False)

        self.assertIs(result, completed)
        argv = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertEqual(
            argv,
            ["git", "--no-pager", "-c", "core.fsmonitor=false",
             "status", "--porcelain=v1"],
        )
        self.assertEqual(
            kwargs["owner_child_kind"], repo_owner.ChildKind.GIT)
        self.assertEqual(kwargs["env"]["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(kwargs["env"]["GIT_PAGER"], "cat")
        self.assertEqual(kwargs["env"]["PAGER"], "cat")

        with mock.patch.dict(os.environ, {"GIT_EXTERNAL_DIFF": "unsafe"}):
            with mock.patch.object(
                    loop_mod, "sh", return_value=completed) as diff_run:
                loop_mod.git(self.repo, "diff", "--", "tracked.txt")
        diff_argv = diff_run.call_args.args[0]
        self.assertEqual(
            diff_argv[4:7], ["diff", "--no-ext-diff", "--no-textconv"])
        self.assertNotIn("GIT_EXTERNAL_DIFF", diff_run.call_args.kwargs["env"])

    def test_reads_disable_fsmonitor_external_diff_and_pager_but_keep_output(self):
        helper, called = self._external_helper()
        _git(self.repo, "config", "core.fsmonitor", str(helper))
        _git(self.repo, "config", "diff.external", str(helper))
        _git(self.repo, "config", "pager.log", str(helper))
        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        index_path = self.repo / ".git" / "index"
        index_before = index_path.read_bytes()
        fence = self._claim()
        running = []
        original_publish = repo_owner.RepoOwnerFence.publish_child_running

        def observe_publish(owner, generation, identity):
            marker = original_publish(owner, generation, identity)
            running.append(repo_owner.RepoOwnerFence.inspect(self.repo))
            return marker

        with mock.patch.object(
                repo_owner.RepoOwnerFence, "publish_child_running",
                new=observe_publish):
            with mock.patch.dict(
                    os.environ, {"GIT_EXTERNAL_DIFF": str(helper)}):
                status = loop_mod.git(
                    self.repo, "status", "--porcelain=v1")
                diff = loop_mod.git(self.repo, "diff", "--", "tracked.txt")
                log = loop_mod.git(self.repo, "log", "-1", "--pretty=%s")

        self.assertIn("tracked.txt", status.stdout)
        self.assertIn("-one", diff.stdout)
        self.assertIn("+two", diff.stdout)
        self.assertEqual(log.stdout.strip(), "initial")
        self.assertFalse(called.exists(), "a configured external helper ran")
        self.assertEqual(index_path.read_bytes(), index_before)
        self.assertEqual(len(running), 3)
        for marker in running:
            self.assertEqual(marker["state"], "active")
            self.assertEqual(marker["child_state"], "child_running")
            self.assertEqual(marker["child_kind"], repo_owner.ChildKind.GIT.value)
        self.assertEqual(fence.marker["child_state"], "idle")

    def test_ralph_git_projection_uses_the_same_controlled_read(self):
        fence = self._claim(repo_owner.OwnerKind.RALPH)
        running = []
        original_publish = repo_owner.RepoOwnerFence.publish_child_running

        def observe_publish(owner, generation, identity):
            marker = original_publish(owner, generation, identity)
            running.append(repo_owner.RepoOwnerFence.inspect(self.repo))
            return marker

        with mock.patch.object(
                repo_owner.RepoOwnerFence, "publish_child_running",
                new=observe_publish):
            head = ralph_mod._git_out(self.repo, "rev-parse", "HEAD")

        self.assertEqual(head, _git(self.repo, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["child_kind"], repo_owner.ChildKind.GIT.value)
        self.assertEqual(running[0]["child_state"], "child_running")
        self.assertEqual(fence.marker["child_state"], "idle")

    def test_parallel_launcher_start_reads_use_controlled_git_children(self):
        fence = self._claim(repo_owner.OwnerKind.PARALLEL_LAUNCHER)
        running = []
        original_publish = repo_owner.RepoOwnerFence.publish_child_running

        def observe_publish(owner, generation, identity):
            marker = original_publish(owner, generation, identity)
            running.append(repo_owner.RepoOwnerFence.inspect(self.repo))
            return marker

        with mock.patch.object(
                repo_owner.RepoOwnerFence, "publish_child_running",
                new=observe_publish):
            branch, head = parallel._repository_start_identity(
                self.repo, owner_fence=fence)

        self.assertTrue(branch.startswith("refs/heads/"))
        self.assertEqual(head, _git(self.repo, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual(len(running), 3)
        for marker in running:
            self.assertEqual(marker["child_kind"], repo_owner.ChildKind.GIT.value)
            self.assertEqual(marker["child_state"], "child_running")
        self.assertEqual(fence.marker["child_state"], "idle")

    def test_ralph_projection_never_raw_spawns_beside_active_payload(self):
        fence = self._claim(repo_owner.OwnerKind.RALPH)
        payload = fence.spawn_child(
            repo_owner.ChildKind.AGENT,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        before = repo_owner.RepoOwnerFence.inspect(self.repo)
        try:
            with mock.patch.object(
                    loop_mod.subprocess, "run",
                    side_effect=AssertionError("raw Git fallback")):
                self.assertEqual(
                    ralph_mod._git_out(self.repo, "rev-parse", "HEAD"), "")
            self.assertEqual(repo_owner.RepoOwnerFence.inspect(self.repo), before)
        finally:
            payload.kill_containment()
            payload.record_result(containment_timeout=5.0)
            fence.checkpoint_child(payload.child_generation)

    def test_owner_hard_kill_reaps_git_read_descendants_and_keeps_evidence(self):
        ready = self.root / "read-ready"
        late_write = self.root / "escaped-read-grandchild"
        payload = self.root / "slow-read.py"
        child_code = (
            "import pathlib,sys,time; time.sleep(1.5); "
            "pathlib.Path(sys.argv[1]).write_text('escaped',encoding='utf-8')"
        )
        payload.write_text(
            "import os,pathlib,subprocess,sys,time\n"
            "assert os.environ.get('GIT_OPTIONAL_LOCKS') == '0'\n"
            "assert os.environ.get('GIT_PAGER') == 'cat'\n"
            f"code={child_code!r}\n"
            "subprocess.Popen([sys.executable,'-c',code,sys.argv[2]],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL)\n"
            "pathlib.Path(sys.argv[1]).write_text('ready',encoding='utf-8')\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        runner = self.root / "read-owner.py"
        runner.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from engine import loop,repo_owner\n"
            "repo,workspace,state,payload,ready,late=map(Path,sys.argv[1:])\n"
            "fence=repo_owner.RepoOwnerFence.claim("
            "repo,owner_kind=repo_owner.OwnerKind.LOOP,workspace=workspace,"
            "state_path=state)\n"
            "loop._REPO_OWNER_FENCE=fence\n"
            "real_sh=loop.sh\n"
            "def redirect(_args,cwd,check=True,*,owner_child_kind=None,env=None):\n"
            " return real_sh([sys.executable,str(payload),str(ready),str(late)],"
            "cwd,check,owner_child_kind=owner_child_kind,env=env)\n"
            "loop.sh=redirect\n"
            "loop.git(repo,'status','--porcelain=v1')\n",
            encoding="utf-8",
        )
        env = {
            **os.environ,
            "PYTHONPATH": str(PROJECT_ROOT),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        owner = subprocess.Popen(
            [sys.executable, str(runner), str(self.repo), str(self.workspace),
             str(self.state_path), str(payload), str(ready), str(late_write)],
            cwd=PROJECT_ROOT, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def cleanup_owner():
            if owner.poll() is None:
                owner.kill()
            try:
                owner.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

        self.addCleanup(cleanup_owner)
        deadline = time.monotonic() + 15
        while (not ready.exists() and owner.poll() is None
               and time.monotonic() < deadline):
            time.sleep(0.05)
        self.assertTrue(ready.exists(), "controlled Git read never started")
        marker = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(marker["state"], "active")
        self.assertEqual(marker["child_state"], "child_running")
        self.assertEqual(marker["child_kind"], repo_owner.ChildKind.GIT.value)
        identity = marker["child_identity"]

        owner.kill()
        owner.wait(timeout=10)
        # Windows closes the inherited kill-on-close Job handles during owner
        # teardown, but descendant termination can be observed asynchronously
        # under a heavily loaded full suite.  Keep the sentinel's 1.5-second
        # escape window, then poll the exact PID creation token to a bound.
        observe_after = time.monotonic() + 2.0
        reap_deadline = time.monotonic() + 10.0
        surviving_token = identity["creation_token"]
        while time.monotonic() < reap_deadline:
            try:
                surviving_token = repo_owner.process_creation_token(
                    identity["pid"])
            except (OSError, repo_owner.RepoOwnerError):
                surviving_token = None
            if (surviving_token != identity["creation_token"]
                    and time.monotonic() >= observe_after):
                break
            time.sleep(0.05)
        self.assertFalse(
            late_write.exists(), "Git read grandchild escaped owner containment")
        self.assertNotEqual(surviving_token, identity["creation_token"])
        after = repo_owner.RepoOwnerFence.inspect(self.repo)
        self.assertEqual(after["state"], "active")
        self.assertEqual(after["child_state"], "child_running")
        self.assertEqual(after["child_kind"], repo_owner.ChildKind.GIT.value)


if __name__ == "__main__":
    unittest.main()

"""L4 harness 的失敗取證契約；確保 gate 失敗時不會只留下一段例外文字。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile

from tests.dry_run import run_full_project as harness


class TestDryRunEvidence(unittest.TestCase):
    @staticmethod
    def process_alive(pid):
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0 and bool(result.stdout.strip()) and \
            not result.stdout.strip().startswith("Z")

    def wait_process_gone(self, pid, timeout=5):
        deadline = time.monotonic() + timeout
        while self.process_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.process_alive(pid), f"pid {pid} should be gone")

    def test_failed_command_keeps_record_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "failed.log"
            with self.assertRaises(harness.CommandFailure) as raised:
                harness.run(
                    [sys.executable, "-c", "print('expected failure'); raise SystemExit(7)"],
                    log=log,
                )
            self.assertEqual(raised.exception.record["exit_code"], 7)
            self.assertEqual(raised.exception.record["log"], str(log))
            self.assertIn("expected failure", log.read_text(encoding="utf-8"))

    def test_timed_out_command_keeps_record_and_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "timeout.log"
            with self.assertRaises(harness.CommandFailure) as raised:
                harness.run([sys.executable, "-c", "import time; time.sleep(2)"],
                            log=log, timeout=0.05)
            self.assertEqual(raised.exception.record["exit_code"], 124)
            self.assertTrue(raised.exception.record["timed_out"])
            self.assertIn("timeout", log.read_text(encoding="utf-8"))

    def test_timeout_kills_the_whole_isolated_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "child.pid"
            script = (
                "import pathlib,signal,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)'])\n"
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                "time.sleep(30)\n"
            )
            with self.assertRaises(harness.CommandFailure) as raised:
                harness.run([sys.executable, "-c", script, str(pid_file)], timeout=0.2)
            self.assertTrue(raised.exception.record["timed_out"])
            self.assertTrue(raised.exception.record["process_group_cleanup"]["sent_kill"])
            self.assertTrue(raised.exception.record["process_group_cleanup"]["group_empty"])
            child_pid = int(pid_file.read_text())
            self.wait_process_gone(child_pid)

    def test_keyboard_interrupt_kills_the_whole_isolated_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file, log = root / "command-pids.json", root / "cancel.log"
            command_script = root / "command.py"
            command_script.write_text(
                "import json,os,pathlib,signal,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)'])\n"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({'parent':os.getpid(),'child':child.pid}))\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            driver = root / "driver.py"
            driver.write_text(
                "import sys\n"
                "from tests.dry_run import run_full_project as harness\n"
                "harness.run([sys.executable,sys.argv[1],sys.argv[2]],log=sys.argv[3])\n",
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [sys.executable, str(driver), str(command_script), str(pid_file), str(log)],
                cwd=harness.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**os.environ, "PYTHONPATH": str(harness.ROOT)},
            )
            deadline = time.monotonic() + 5
            while not pid_file.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.is_file(), "isolated command did not start")
            pids = json.loads(pid_file.read_text(encoding="utf-8"))
            os.kill(process.pid, __import__("signal").SIGINT)
            process.wait(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("cancelled by KeyboardInterrupt", log.read_text(encoding="utf-8"))
            self.wait_process_gone(pids["parent"])
            self.wait_process_gone(pids["child"])

    def test_successful_parent_with_live_child_fails_and_cleans_group(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            script = (
                "import pathlib,subprocess,sys\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
                " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
            )
            with self.assertRaisesRegex(harness.CommandFailure, "live process group") as raised:
                harness.run([sys.executable, "-c", script, str(pid_file)])
            self.assertTrue(raised.exception.record["leaked_process_group"])
            self.assertTrue(raised.exception.record["process_group_cleanup"]["group_empty"])
            self.wait_process_gone(int(pid_file.read_text()))

    def test_parallel_snapshot_survives_parent_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, artifacts = root / "parent", root / "artifacts"
            parent.mkdir()
            fleet = {
                "run_id": "a" * 32,
                "phase": "done",
                "integration_ref": "refs/heads/main",
                "expected_integration_sha": "b" * 40,
                "tracks": [{"name": "backend", "status": "cleaned", "restart_count": 1,
                            "integration_validate_failures": 2, "diagnostics": {"round": 3}}],
            }
            (parent / "fleet.json").write_text(json.dumps(fleet), encoding="utf-8")
            (parent / "REPORT.md").write_text("report\n", encoding="utf-8")
            (parent / "console.log").write_text("x" * 600_000, encoding="utf-8")
            manifest = {}
            harness.snapshot_parallel_evidence(parent, artifacts, manifest)
            self.assertEqual(manifest["parallel_run"]["phase"], "done")
            self.assertEqual(manifest["parallel_run"]["tracks"][0]["diagnostics"]["round"], 3)
            self.assertLessEqual((artifacts / "console-tail.log").stat().st_size, 500_000)
            for child in parent.iterdir():
                child.unlink()
            parent.rmdir()
            second = {}
            harness.snapshot_parallel_evidence(parent, artifacts, second)
            self.assertEqual(second["parallel_run"]["final_sha"], "b" * 40)

    def test_truth_snapshot_requires_matching_primary_and_last_good_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, artifacts = root / "parent", root / "artifacts"
            parent.mkdir()
            for primary, checkpoint, payload in (
                    ("fleet.json", "fleet.last-good.json", {"phase": "done"}),
                    ("state.json", "state.last-good.json", {"phase": "done", "round": 3})):
                encoded = json.dumps(payload, indent=2)
                (parent / primary).write_text(encoded, encoding="utf-8")
                (parent / checkpoint).write_text(encoded, encoding="utf-8")
            manifest = {}
            harness.snapshot_parallel_evidence(parent, artifacts, manifest)
            harness.require_parallel_truth_snapshots(artifacts, manifest)
            self.assertEqual(len(manifest["truth_snapshots"]), 4)
            self.assertTrue(all(len(item["sha256"]) == 64
                                for item in manifest["truth_snapshots"]))
            (artifacts / "state.last-good.json").write_text(
                json.dumps({"phase": "stale", "round": 2}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "truth/checkpoint 不一致"):
                harness.require_parallel_truth_snapshots(artifacts, manifest)

    def test_port_reservation_is_visible_to_another_process(self):
        reservation = harness.reserve_loopback_port()
        try:
            self.assertTrue(reservation.lock_path.is_file())
            attempted = subprocess.run([
                sys.executable, "-c",
                ("import os,sys\n"
                 "try: os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)\n"
                 "except FileExistsError: raise SystemExit(23)\n"),
                str(reservation.lock_path),
            ])
            self.assertEqual(attempted.returncode, 23)
        finally:
            reservation.release()
        self.assertFalse(reservation.lock_path.exists())

    def test_dashboard_fixture_identity_must_match_config_override(self):
        process = mock.Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "dashboard.config.local.json"
            responses = [
                {"status": "ok"},
                {"config_override": True, "personal_config_path": str(expected)},
            ]
            with mock.patch.object(harness, "_dashboard_json", side_effect=responses):
                evidence = harness.wait_for_dashboard_fixture(
                    process, "http://127.0.0.1:12345", expected, timeout=0.1)
            self.assertEqual(evidence["config"]["personal_config_path"], str(expected.resolve()))
            responses = [
                {"status": "ok"},
                {"config_override": True,
                 "personal_config_path": str(expected.with_name("other.json"))},
            ]
            with mock.patch.object(harness, "_dashboard_json", side_effect=responses), \
                    self.assertRaisesRegex(RuntimeError, "非本次 L4 fixture"):
                harness.wait_for_dashboard_fixture(
                    process, "http://127.0.0.1:12345", expected, timeout=0.1)

    def test_dashboard_command_uses_installed_public_entrypoint(self):
        command = harness.dashboard_command(Path("/tmp/venv/bin/loop"), 45678)
        self.assertEqual(command, ["/tmp/venv/bin/loop", "dashboard", "--port", "45678"])

    def test_full_project_validator_has_a_bounded_long_run_timeout(self):
        self.assertGreaterEqual(harness.L4_VALIDATE_TIMEOUT_SECONDS, 10 * 60)
        self.assertLess(harness.L4_VALIDATE_TIMEOUT_SECONDS, harness.PLAYWRIGHT_TOTAL_SECONDS)
        self.assertGreaterEqual(harness.L4_PLANNING_TIMEOUT_SECONDS, 60 * 60)
        self.assertLess(harness.L4_PLANNING_TIMEOUT_SECONDS,
                        harness.PLAYWRIGHT_TOTAL_SECONDS - 60 * 60)

    def test_scoped_cleanup_runs_out_of_process_and_verifies_empty_remaining(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            env = {
                **harness.sanitized_child_environment(),
                "LOOP_AGENT_WORKSPACE_ROOT": str(workspace),
                "LOOP_AGENT_HOME": str(root / "home"),
            }
            payload, record = harness.run_scoped_coordinator_cleanup(
                Path(sys.executable), harness.ROOT, env, root / "cleanup.log", "l4-empty")
            self.assertEqual(payload["remaining"], [])
            self.assertEqual(record["exit_code"], 0)
            self.assertIn('"remaining": []', (root / "cleanup.log").read_text())

    def test_scoped_cleanup_finds_pre_state_fixture_and_rejects_same_name_other_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            fake = root / "fake"
            package = fake / "engine"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "loop.py").write_text(
                "import json,os,pathlib,signal,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)'])\n"
                "pathlib.Path(os.environ['PID_FILE']).write_text(json.dumps({'child': child.pid}))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            name = "l4-pre-state"
            processes = []

            def spawn(repo, pid_file):
                environment = {**os.environ, "PYTHONPATH": str(fake), "PID_FILE": str(pid_file)}
                process = subprocess.Popen(
                    [sys.executable, "-m", "engine.loop", "--repo", str(repo), "--name", name],
                    cwd=fake, env=environment, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
                processes.append(process)
                deadline = time.monotonic() + 5
                while not pid_file.is_file() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(pid_file.is_file(), "fake coordinator did not start")
                return process, json.loads(pid_file.read_text())["child"]

            exact_file, wrong_file = root / "exact.pid", root / "wrong.pid"
            exact, exact_child = spawn(harness.ROOT, exact_file)
            wrong_repo = root / "other-clone"
            wrong_repo.mkdir()
            wrong, wrong_child = spawn(wrong_repo, wrong_file)
            env = {
                **harness.sanitized_child_environment(),
                "LOOP_AGENT_WORKSPACE_ROOT": str(workspace),
                "LOOP_AGENT_HOME": str(root / "home"),
            }
            try:
                payload, record = harness.run_scoped_coordinator_cleanup(
                    Path(sys.executable), harness.ROOT, env, root / "cleanup.log", name)
                self.assertEqual(record["exit_code"], 0)
                self.assertIn(exact.pid, payload["cleanup"]["fixture_roots"])
                self.assertNotIn(wrong.pid, payload["cleanup"]["fixture_roots"])
                exact.wait(timeout=5)
                self.wait_process_gone(exact_child)
                self.assertTrue(self.process_alive(wrong.pid))
                self.assertTrue(self.process_alive(wrong_child))
            finally:
                for process in processes:
                    if process.poll() is None:
                        try:
                            os.killpg(process.pid, __import__("signal").SIGKILL)
                        except ProcessLookupError:
                            pass
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass

    def test_dr1_goal_is_clone_only_and_exact(self):
        text = harness.goal_text("dr1")
        self.assertIn("engine/l4_delivery_probe.py", text)
        self.assertIn("ui/src/features/workspaces/l4DeliveryProbe.ts", text)
        self.assertIn("Parallel tracks ${done}/${total} merged", text)
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            harness.assert_dr1_contract_absent(repo)
            path = repo / "engine" / "l4_delivery_probe.py"
            path.parent.mkdir()
            path.write_text("already shipped\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "不再 deterministic"):
                harness.assert_dr1_contract_absent(repo)

    def test_l4_dashboard_environment_does_not_leak_fixture_metadata_to_agents(self):
        base = {
            "PATH": "/test/bin",
            "OPENAI_API_KEY": "must-not-leak",
            "LOOP_L4_PLAN": "stale adversarial plan",
            "LOOP_L4_SCENARIO": "stale",
            "LOOP_WS": "/stale/workspace",
        }
        common = {
            "workspace": Path("/isolated/workspace"),
            "home": Path("/isolated/home"),
            "config": Path("/isolated/config.json"),
            "base_url": "http://127.0.0.1:45678",
            "repo": Path("/isolated/repo"),
            "artifacts": Path("/isolated/artifacts"),
            "validate_cmd": "python3 -m unittest",
        }

        original = dict(base)
        dr1_dashboard, dr1_playwright, dr1_delete = harness.l4_process_environments(
            base, scenario="dr1", **common)
        dr2_dashboard, dr2_playwright, dr2_delete = harness.l4_process_environments(
            base, scenario="dr2", **common)

        self.assertEqual(base, original)
        self.assertEqual(dr1_dashboard, dr2_dashboard)
        self.assertEqual(dr1_dashboard["PATH"], "/test/bin")
        self.assertNotIn("OPENAI_API_KEY", dr1_dashboard)
        self.assertFalse(any(name.startswith("LOOP_L4_") for name in dr1_dashboard))
        self.assertNotIn("LOOP_WS", dr1_dashboard)
        self.assertFalse(any(name.startswith("LOOP_AGENT_") for name in dr1_playwright))
        self.assertEqual(dr1_playwright["LOOP_L4_SCENARIO"], "dr1")
        self.assertIn("LOOP_L4_PLANNING_TIMEOUT", dr1_playwright)
        self.assertNotIn("LOOP_L4_PLAN", dr1_playwright)
        self.assertEqual(dr2_playwright["LOOP_L4_SCENARIO"], "dr2")
        self.assertEqual(json.loads(dr2_playwright["LOOP_L4_PLAN"]), harness.dr2_plan())
        self.assertNotIn("LOOP_L4_PLANNING_TIMEOUT", dr2_playwright)
        self.assertEqual(
            {name for name in dr1_delete if name.startswith("LOOP_L4_")},
            {"LOOP_L4_BASE_URL", "LOOP_L4_SCENARIO", "LOOP_L4_ARTIFACTS",
             "LOOP_L4_DELETE_PHASE"})
        self.assertEqual(dr1_delete["LOOP_L4_SCENARIO"], "dr1")
        self.assertEqual(dr2_delete["LOOP_L4_SCENARIO"], "dr2")
        with self.assertRaisesRegex(ValueError, "unsupported L4 scenario"):
            harness.l4_process_environments(base, scenario="unexpected", **common)

    def test_integration_fault_validator_is_dr2_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clone, fixture = root / "clone", root / "harness"
            source = clone / "tests" / "dry_run" / "integration_validator.py"
            source.parent.mkdir(parents=True)
            source.write_text("raise SystemExit('fault')\n", encoding="utf-8")
            fixture.mkdir()

            dr1_validator, dr1_hash = harness.prepare_integration_validator(
                "dr1", clone, fixture)
            self.assertIsNone(dr1_validator)
            self.assertIsNone(dr1_hash)
            self.assertEqual(list(fixture.iterdir()), [])
            dr1_command = harness.l4_validate_command(Path("/venv/python"), dr1_validator)
            self.assertIn("unittest discover", dr1_command)
            self.assertIn("npm run check", dr1_command)
            self.assertNotIn("integration_validator", dr1_command)

            dr2_validator, dr2_hash = harness.prepare_integration_validator(
                "dr2", clone, fixture)
            self.assertEqual(dr2_validator, fixture / "integration_validator.py")
            self.assertEqual(dr2_hash, harness.sha256_file(dr2_validator))
            dr2_command = harness.l4_validate_command(Path("/venv/python"), dr2_validator)
            self.assertIn(str(dr2_validator), dr2_command)

    def test_dr1_ui_contract_is_checked_after_production_bundle_reload(self):
        spec = (harness.ROOT / "ui" / "e2e" / "parallel-real-dry-run.spec.ts").read_text(
            encoding="utf-8")
        track_creation = spec.index('await screenshot(page, testInfo, "03-tracks-created")')
        bundle_reload = spec.index("await page.reload();", track_creation)
        assertion = ('"aria-label", `Parallel tracks '
                     '${completedTrackCount}/${completedTrackCount} merged`')
        self.assertEqual(spec.count(assertion), 1)
        contract_assertion = spec.index(assertion)
        self.assertGreater(contract_assertion, bundle_reload)

    def test_real_ui_scenarios_fail_fast_on_fixture_cross_contamination(self):
        spec = (harness.ROOT / "ui" / "e2e" / "parallel-real-dry-run.spec.ts").read_text(
            encoding="utf-8")
        for contract in (
                'scenario !== "dr1" && scenario !== "dr2"',
                'DR-1 must not receive LOOP_L4_PLAN',
                'DR-2 requires LOOP_L4_PLAN',
                'DR-2 must not receive LOOP_L4_PLANNING_TIMEOUT'):
            self.assertIn(contract, spec)

    def test_playwright_index_is_bounded_to_audit_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            (artifacts / "nested").mkdir()
            (artifacts / "nested" / "trace.zip").write_bytes(b"trace")
            (artifacts / "nested" / "video.webm").write_bytes(b"video")
            (artifacts / "nested" / "screen.png").write_bytes(b"screen")
            (artifacts / "large-third-party.js").write_text("ignored", encoding="utf-8")
            manifest = {}
            harness.index_playwright_artifacts(artifacts, manifest)
            self.assertEqual(
                {item["kind"] for item in manifest["playwright_artifacts"]},
                {"trace", "video", "screenshot"},
            )
            self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["playwright_artifacts"]))

    def test_release_gate_requires_each_artifact_kind_for_run_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            manifest = {}
            for phase in ("playwright-run", "playwright-delete"):
                target = artifacts / phase / "case"
                target.mkdir(parents=True)
                (target / "trace.zip").write_bytes(b"trace")
                (target / "video.webm").write_bytes(b"video")
                (target / "screen.png").write_bytes(b"screen")
            harness.require_playwright_artifacts(artifacts, manifest)
            (artifacts / "playwright-delete" / "case" / "trace.zip").unlink()
            with self.assertRaisesRegex(RuntimeError, "playwright-delete/trace"):
                harness.require_playwright_artifacts(artifacts, manifest)
            (artifacts / "playwright-delete" / "case" / "trace.zip").write_bytes(b"")
            with self.assertRaisesRegex(RuntimeError, "playwright-delete/trace"):
                harness.require_playwright_artifacts(artifacts, manifest)

    def test_codex_override_is_explicitly_reported(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict("os.environ", {"LOOP_L4_CODEX_CMD": " codex   exec -m gpt-5.4 "}):
            metadata = harness.configured_codex(Path(directory))
            self.assertEqual(metadata["command"], "codex exec -m gpt-5.4")
            self.assertEqual(metadata["model"], "gpt-5.4")
            self.assertEqual(metadata["source"], "environment_command_override")

    def test_codex_command_uses_shlex_join_and_redacts_inline_secret(self):
        metadata = harness.codex_command_metadata(
            'codex exec --model "gpt 5" --api-key "do not persist"', "test")
        self.assertEqual(metadata["model"], "gpt 5")
        self.assertIn("'gpt 5'", metadata["command"])
        self.assertNotIn("do not persist", metadata["manifest_command"])
        self.assertIn("<redacted>", metadata["manifest_command"])
        self.assertTrue(metadata["contains_sensitive_value"])

    def test_sensitive_host_environment_is_removed_and_text_artifact_fails_closed(self):
        secret = "l4-secret-value-that-must-not-survive"
        secret_key_base = "rails-secret-key-base-value"
        client_secret_json = '{"client_secret":"oauth-secret-value"}'
        raw = {"PATH": "/safe/bin", "HOME": "/safe/home", "OPENAI_API_KEY": secret,
               "SECRET_KEY_BASE": secret_key_base, "CLIENT_SECRET_JSON": client_secret_json,
               "MONKEY": "banana", "KEYBOARD_LAYOUT": "dvorak",
               "LOOP_WS": "outer", "LOOP_ROUND_TOKEN": "round", "LOOP_FLEET_TRACK": "backend",
               "LOOP_AGENT_WORKSPACE_ROOT": "/unsafe", "LOOP_TRACK_PORT": "8876",
               "LOOP_L4_BASE_URL": "http://stale.invalid"}
        values = harness.sensitive_environment_values(raw)
        sanitized = harness.sanitized_child_environment(raw)
        self.assertEqual(sanitized, {"PATH": "/safe/bin", "HOME": "/safe/home",
                                     "MONKEY": "banana", "KEYBOARD_LAYOUT": "dvorak"})
        self.assertIn(secret.encode(), values)
        self.assertIn(secret_key_base.encode(), values)
        self.assertIn(client_secret_json.encode(), values)
        self.assertIn(b"round", values)
        self.assertNotIn(b"banana", values)
        self.assertNotIn(b"dvorak", values)
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            leaked = artifacts / "agent.log"
            leaked.write_text(f"tool output: {secret}\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "agent.log") as raised:
                harness.assert_artifacts_contain_no_sensitive_values(artifacts, values)
            self.assertNotIn(secret, str(raised.exception))
            self.assertFalse(leaked.exists())

    def test_sensitive_value_in_zip_entry_is_removed_without_disclosure(self):
        secret = "l4-zip-secret-value"
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            trace = artifacts / "trace.zip"
            with zipfile.ZipFile(trace, "w") as archive:
                archive.writestr("trace.network", f"authorization={secret}")
            with self.assertRaisesRegex(RuntimeError, r"trace\.zip::trace\.network") as raised:
                harness.assert_artifacts_contain_no_sensitive_values(
                    artifacts, harness.sensitive_environment_values({"ACCESS_TOKEN": secret}))
            self.assertNotIn(secret, str(raised.exception))
            self.assertFalse(trace.exists())

    def test_configured_codex_reports_default_personal_source(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", {}, clear=True):
            source = Path(directory)
            (source / "dashboard.config.local.json").write_text(json.dumps({
                "agent_cmds": [{"label": "codex", "cmd": "codex exec -m gpt-5.4"}],
            }), encoding="utf-8")
            metadata = harness.configured_codex(source)
            self.assertEqual(metadata["source"], "personal_label_codex")
            self.assertEqual(metadata["manifest_command"], "codex exec -m gpt-5.4")

    def test_git_snapshot_records_each_read_only_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, artifacts = root / "repo", root / "artifacts"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "harness@test.local"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "harness test"], cwd=repo, check=True)
            (repo / "tracked.txt").write_text("ok\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
            manifest = {"commands": []}
            harness.snapshot_git_evidence(repo, artifacts, manifest)
            self.assertEqual(len(manifest["commands"]), 4)
            self.assertTrue((artifacts / "git-status.log").is_file())
            self.assertTrue(all(item["exit_code"] == 0 for item in manifest["commands"]))

    def test_source_checkout_must_keep_original_head_and_clean_state(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "harness@test.local"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "harness test"], cwd=repo, check=True)
            tracked = repo / "tracked.txt"
            tracked.write_text("baseline\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
            expected = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, text=True,
                                      capture_output=True, check=True).stdout.strip()
            harness.assert_source_checkout_unchanged(repo, expected)
            tracked.write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "HEAD 或工作樹已改變"):
                harness.assert_source_checkout_unchanged(repo, expected)
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "concurrent change"], cwd=repo, check=True)
            with self.assertRaisesRegex(RuntimeError, "HEAD 或工作樹已改變"):
                harness.assert_source_checkout_unchanged(repo, expected)


class TestDryRunScenarioGate(unittest.TestCase):
    command = "codex exec --dangerously-bypass-approvals-and-sandbox -m gpt-5.4"
    validate_command = "python3 -m unittest discover -s tests -t . -q"

    def git(self, repo, *args, check=True):
        result = subprocess.run(["git", *args], cwd=repo, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if check and result.returncode:
            self.fail(f"git {' '.join(args)} failed:\n{result.stdout}")
        return result

    def init_repo(self, root):
        repo = root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.email", "harness@test.local")
        self.git(repo, "config", "user.name", "harness test")
        return repo

    def commit_many(self, repo, files, message):
        for relative, content in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            self.git(repo, "add", relative)
        self.git(repo, "commit", "-qm", message)
        return self.git(repo, "rev-parse", "HEAD").stdout.strip()

    def commit(self, repo, relative, content, message):
        return self.commit_many(repo, {relative: content}, message)

    @staticmethod
    def success_history(track, candidate, expected):
        return [{"track": track, "candidate_sha": candidate, "expected_sha": expected,
                 "stage": stage} for stage in ("prepared", "ref-updated", "validating", "validated")]

    def base_state(self, initial, final, plan, tracks, phases, history):
        return {
            "phase": "done", "initial_integration_sha": initial,
            "expected_integration_sha": final,
            "config": {"flag_threshold": 10, "done_threshold": 3, "merge_threshold": 2,
                       "max_parallel": 4, "agent_cmd": self.command,
                       "validate_cmd": self.validate_command,
                       "validate_timeout": harness.L4_VALIDATE_TIMEOUT_SECONDS},
            "plan": plan, "tracks": tracks,
            "phase_history": [{"phase": phase} for phase in phases],
            "merge_history": history,
        }

    def make_dr1(self, root, *, valid_delivery=True):
        repo = self.init_repo(root)
        initial = self.commit(repo, "base.txt", "base\n", "base")
        backend_files = ({
            "engine/l4_delivery_probe.py": (
                "def summarize_l4_parallel_phases(phases):\n"
                "    result = []\n"
                "    for phase in phases:\n"
                "        if not result or result[-1] != phase: result.append(phase)\n"
                "    return ' → '.join(result)\n"),
            "tests/test_l4_delivery_probe.py": (
                "def test_l4_delivery_probe():\n"
                "    from engine.l4_delivery_probe import summarize_l4_parallel_phases as f\n"
                "    assert f(['planning', 'planning', 'done']) == 'planning → done'\n"),
        }
                         if valid_delivery else {"docs/backend-marker.txt": "backend\n"})
        frontend_files = ({
            "ui/src/features/workspaces/l4DeliveryProbe.ts": (
                "export const l4TrackProgressLabel = (done: number, total: number) => "
                "`Parallel tracks ${done}/${total} merged`;\n"),
            "ui/src/features/workspaces/ParallelRunGroup.tsx": "// uses l4TrackProgressLabel\n",
            "ui/e2e/dashboard-flow.spec.ts": "// verifies deterministic L4 aria-label\n",
            "engine/ui/l4-frontend.js": "globalThis.l4TrackProgress = true;\n",
        }
                          if valid_delivery else {"docs/frontend-marker.txt": "frontend\n"})
        backend = self.commit_many(repo, backend_files, "backend")
        frontend = self.commit_many(repo, frontend_files, "frontend")
        final = self.commit(repo, "final.txt", "ok\n", "final")
        refs = {"backend": backend, "frontend": frontend, "@final": final}
        for index, (name, sha) in enumerate(refs.items(), 1):
            safe = harness.safe_track_name(name)
            self.git(repo, "branch", f"loop/test/{safe}", sha)
        tracks = [{"name": name, "tip": sha, "status": "cleaned", "index": index,
                   "port": 45100 + index,
                   "env": {"TMPDIR": f"/tmp/dr1/{harness.safe_track_name(name)}/tmp",
                           "XDG_CACHE_HOME": f"/tmp/dr1/{harness.safe_track_name(name)}/cache",
                           "npm_config_cache": f"/tmp/dr1/{harness.safe_track_name(name)}/npm",
                           "LOOP_TRACK_NAME": name, "LOOP_TRACK_INDEX": str(index),
                           "LOOP_TRACK_PORT": str(45100 + index)},
                   "child_workspace": f"l4-dr1--{harness.safe_track_name(name)}",
                   "branch_ref": f"refs/heads/loop/test/{harness.safe_track_name(name)}",
                   "integration_validate_failures": 0,
                   "status_history": ([{"status": "running"}, {"status": "stopped"},
                                       {"status": "running"}, {"status": "cleaned"}]
                                      if name != "@final" else
                                      [{"status": "running"}, {"status": "cleaned"}])}
                  for index, (name, sha) in enumerate(refs.items(), 1)]
        plan = [
            {"order": 1, "track": "backend", "task": "backend engine helper", "scope": ["engine/**"]},
            {"order": 2, "track": "frontend", "task": "frontend labels", "scope": ["ui/**"]},
            {"order": 3, "track": "@final", "task": "full validation"},
        ]
        history = []
        expected = initial
        for index, (name, sha) in enumerate(refs.items(), 1):
            history.extend(self.success_history(name, sha, expected))
            expected = sha
        state = self.base_state(
            initial, final, plan, tracks,
            ["planning", "splitting", "exec", "stopping", "stopped", "exec",
             "merging", "exec", "final", "merging", "final", "cleaning", "done"], history)
        state["integration_ref"] = "refs/heads/" + self.git(
            repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
        return repo, state

    def make_dr2(self, root):
        repo = self.init_repo(root)
        initial = self.commit(repo, "docs/dr2-shared.txt", "baseline\n", "base")
        base_branch = self.git(repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
        self.git(repo, "switch", "-qc", "track-a")
        commit_a = self.commit(repo, "docs/dr2-shared.txt", "track A\n", "track A")
        self.git(repo, "switch", "-q", base_branch)
        self.git(repo, "switch", "-qc", "track-b")
        commit_b = self.commit(repo, "docs/dr2-shared.txt", "track B\n", "track B")
        self.git(repo, "switch", "-q", "track-a")
        merged = self.git(repo, "merge", "--no-edit", "track-b", check=False)
        self.assertNotEqual(merged.returncode, 0, "fixture must produce a real conflict")
        failed = self.commit(repo, "docs/dr2-shared.txt", "track A and track B resolved\n", "resolve conflict")
        repaired = self.commit(repo, "docs/dr2-compat.txt", "integration invariant repaired\n", "repair")
        final = self.commit(repo, "final.txt", "ok\n", "final")
        refs = {"conflict-a": commit_a, "conflict-b": repaired, "@final": final}
        for name, sha in refs.items():
            safe = harness.safe_track_name(name)
            self.git(repo, "branch", f"loop/test/{safe}", sha)
        tracks = []
        for index, (name, sha) in enumerate(refs.items(), 1):
            repair = name == "conflict-b"
            tracks.append({
                "name": name, "tip": sha, "status": "cleaned",
                "index": index, "port": 45200 + index,
                "env": {"TMPDIR": f"/tmp/dr2/{harness.safe_track_name(name)}/tmp",
                        "XDG_CACHE_HOME": f"/tmp/dr2/{harness.safe_track_name(name)}/cache",
                        "npm_config_cache": f"/tmp/dr2/{harness.safe_track_name(name)}/npm",
                        "LOOP_TRACK_NAME": name, "LOOP_TRACK_INDEX": str(index),
                        "LOOP_TRACK_PORT": str(45200 + index)},
                "child_workspace": f"l4-dr2--{harness.safe_track_name(name)}",
                "branch_ref": f"refs/heads/loop/test/{harness.safe_track_name(name)}",
                "integration_validate_failures": 1 if repair else 0,
                "last_integration_error": "integration invariant" if repair else None,
                "status_history": ([{"status": "repairing"}] if repair else []),
            })
        plan = harness.dr2_plan()
        history = self.success_history("conflict-a", commit_a, initial)
        history.extend([
            {"track": "conflict-b", "candidate_sha": failed, "expected_sha": commit_a,
             "stage": stage, **({"validation_error": "integration invariant"}
                                if stage == "rollback-prepared" else {})}
            for stage in ("prepared", "ref-updated", "validating", "rollback-prepared", "rolled-back")
        ])
        history.extend(self.success_history("conflict-b", repaired, commit_a))
        history.extend(self.success_history("@final", final, repaired))
        state = self.base_state(
            initial, final, plan, tracks,
            ["planning", "splitting", "exec", "merging", "exec", "final",
             "merging", "final", "cleaning", "done"], history)
        state["integration_ref"] = "refs/heads/" + self.git(
            repo, "symbolic-ref", "--short", "HEAD").stdout.strip()
        return repo, state, {"a": commit_a, "b": commit_b, "failed": failed, "repaired": repaired}

    def test_dr1_requires_real_defaults_planning_stop_resume_and_independent_tracks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, state = self.make_dr1(root)
            workspace = root / "workspace"
            workspace.mkdir()
            evidence = harness.validate_parallel_run_evidence(
                state, scenario="dr1", repo=repo, workspace_root=workspace,
                parent_state={"round": 4}, expected_agent_command=self.command,
                expected_validate_command=self.validate_command,
                fixture_sha=state["initial_integration_sha"])
            self.assertEqual(evidence["actual_thresholds"], harness.EXPECTED_THRESHOLDS)
            self.assertEqual(evidence["actual_validate_timeout"],
                             harness.L4_VALIDATE_TIMEOUT_SECONDS)
            self.assertIn("engine/l4_delivery_probe.py", evidence["track_changed_paths"]["backend"])
            self.assertEqual(set(evidence["resumed_child_tracks"]), {"backend", "frontend"})
            broken = json.loads(json.dumps(state))
            broken["config"]["done_threshold"] = 1
            with self.assertRaisesRegex(RuntimeError, "shipped thresholds"):
                harness.validate_parallel_run_evidence(
                    broken, scenario="dr1", repo=repo, workspace_root=workspace,
                    parent_state={"round": 4}, expected_agent_command=self.command,
                    expected_validate_command=self.validate_command,
                    fixture_sha=state["initial_integration_sha"])

            broken = json.loads(json.dumps(state))
            broken["config"]["validate_cmd"] = "python3 -m unittest -q"
            with self.assertRaisesRegex(RuntimeError, "production UI 輸入的完整驗證命令"):
                harness.validate_parallel_run_evidence(
                    broken, scenario="dr1", repo=repo, workspace_root=workspace,
                    parent_state={"round": 4}, expected_agent_command=self.command,
                    expected_validate_command=self.validate_command,
                    fixture_sha=state["initial_integration_sha"])

            broken = json.loads(json.dumps(state))
            broken["config"]["validate_timeout"] = 120
            with self.assertRaisesRegex(RuntimeError, "persisted validate_timeout"):
                harness.validate_parallel_run_evidence(
                    broken, scenario="dr1", repo=repo, workspace_root=workspace,
                    parent_state={"round": 4}, expected_agent_command=self.command,
                    expected_validate_command=self.validate_command,
                    fixture_sha=state["initial_integration_sha"])

    def test_dr1_rejects_matching_plan_words_without_real_delivery_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, state = self.make_dr1(root, valid_delivery=False)
            workspace = root / "workspace"
            workspace.mkdir()
            with self.assertRaisesRegex(RuntimeError, "CAS diff"):
                harness.validate_parallel_run_evidence(
                    state, scenario="dr1", repo=repo, workspace_root=workspace,
                    parent_state={"round": 4}, expected_agent_command=self.command,
                    expected_validate_command=self.validate_command,
                    fixture_sha=state["initial_integration_sha"])

    def test_dr1_requires_ordered_child_stop_then_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, state = self.make_dr1(root)
            workspace = root / "workspace"
            workspace.mkdir()
            for track in state["tracks"]:
                track["status_history"] = [
                    {"status": "running"}, {"status": "cleaned"},
                ]
            with self.assertRaisesRegex(RuntimeError, "running → stopped → running"):
                harness.validate_parallel_run_evidence(
                    state, scenario="dr1", repo=repo, workspace_root=workspace,
                    parent_state={"round": 4}, expected_agent_command=self.command,
                    expected_validate_command=self.validate_command,
                    fixture_sha=state["initial_integration_sha"])

    def test_each_track_requires_unique_npm_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, state = self.make_dr1(root)
            workspace = root / "workspace"
            workspace.mkdir()
            shared = state["tracks"][0]["env"]["npm_config_cache"]
            state["tracks"][1]["env"]["npm_config_cache"] = shared
            with self.assertRaisesRegex(RuntimeError, "runtime/cache"):
                harness.validate_parallel_run_evidence(
                    state, scenario="dr1", repo=repo, workspace_root=workspace,
                    parent_state={"round": 4}, expected_agent_command=self.command,
                    expected_validate_command=self.validate_command,
                    fixture_sha=state["initial_integration_sha"])

    def test_dr2_proves_divergent_conflict_rollback_and_new_repair_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, state, commits = self.make_dr2(root)
            workspace = root / "workspace"
            workspace.mkdir()
            evidence = harness.validate_parallel_run_evidence(
                state, scenario="dr2", repo=repo, workspace_root=workspace,
                parent_state={"round": 0}, expected_agent_command=self.command,
                expected_validate_command=self.validate_command,
                fixture_sha=state["initial_integration_sha"])
            self.assertEqual(evidence["repair"]["failed_candidate"], commits["failed"])
            self.assertEqual(evidence["repair"]["repaired_candidate"], commits["repaired"])
            self.assertEqual(evidence["conflict"]["track_a_commit"], commits["a"])
            broken = json.loads(json.dumps(state))
            broken["merge_history"] = [entry for entry in broken["merge_history"]
                                       if entry.get("stage") != "rolled-back"]
            with self.assertRaisesRegex(RuntimeError, "CAS history|rollback history|rolled-back"):
                harness.validate_parallel_run_evidence(
                    broken, scenario="dr2", repo=repo, workspace_root=workspace,
                    parent_state={"round": 0}, expected_agent_command=self.command,
                    expected_validate_command=self.validate_command,
                    fixture_sha=state["initial_integration_sha"])

    def test_dr2_plan_has_no_synthetic_issue_round(self):
        plan_text = json.dumps(harness.dr2_plan(), ensure_ascii=False)
        self.assertNotIn("先用本輪提供的 issue 命令", plan_text)
        self.assertNotIn("sentinel", plan_text.lower())
        self.assertIn("integration validator", plan_text)

    def test_cleaned_status_cannot_hide_remaining_child_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo, state = self.make_dr1(root)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / state["tracks"][0]["child_workspace"]).mkdir()
            with self.assertRaisesRegex(RuntimeError, "尚未清理"):
                harness.validate_parallel_run_evidence(
                    state, scenario="dr1", repo=repo, workspace_root=workspace,
                    parent_state={"round": 4}, expected_agent_command=self.command,
                    expected_validate_command=self.validate_command,
                    fixture_sha=state["initial_integration_sha"])


class TestParentTrackEvidenceGate(unittest.TestCase):
    command = "codex exec -m gpt-5.4"
    validate_command = "python3 -m unittest discover -s tests -t . -q"

    def build_track(self, parent, name, events):
        safe = harness.safe_track_name(name)
        child = f"parent--{safe}"
        prompt_path = parent / "evidence" / "tracks" / safe / "prompts" / "round-0001.md"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(f"{name} prompt\n", encoding="utf-8")
        evidence = {
            "schema_version": 1, "track": name, "child_workspace": child,
            "captured_at": "2026-07-13T00:00:00+08:00", "state": {"round": len(events)},
            "no_progress_count": 0,
            "agent_command_sha256": __import__("hashlib").sha256(self.command.encode()).hexdigest(),
            "validate_command_sha256": __import__("hashlib").sha256(
                self.validate_command.encode()).hexdigest(),
            "prompt_artifacts": [{"name": prompt_path.name,
                                  "sha256": harness.sha256_file(prompt_path),
                                  "size": prompt_path.stat().st_size}],
            "console_tail": f"{name} console", "history_tail": [f"{name} history"],
            "event_history": events,
        }
        path = parent / "evidence" / "tracks" / safe / "evidence.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence), encoding="utf-8")
        cleanup = [{"event": event, "at": f"later-{index}"} for index, event in enumerate((
            "cleanup-evidence-captured", "cleanup-worktree-removed", "cleanup-child-removed", "cleaned"), 1)]
        return {"name": name, "child_workspace": child, "integration_validate_failures": 0,
                "event_history": [*events, *cleanup],
                "evidence_path": str(path.resolve()),
                "evidence_sha256": harness.sha256_file(path)}

    def test_copies_and_indexes_bounded_track_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, artifacts = root / "parent", root / "artifacts"
            parent.mkdir()
            tracks = [
                self.build_track(parent, "backend", [
                    {"event": "child-state", "phase": "exec", "at": "1"},
                    {"event": "child-state", "phase": "merge", "merge_stage": "sync", "at": "2"},
                    {"event": "child-state", "phase": "merge", "merge_stage": "confirm", "at": "3"},
                ]),
                self.build_track(parent, "@final", [
                    {"event": "child-state", "phase": "exec", "at": "4"},
                    {"event": "child-state", "phase": "merge", "merge_stage": "confirm", "at": "5"},
                ]),
            ]
            indexed = harness.validate_parent_track_evidence(
                parent, artifacts, {"tracks": tracks}, self.command,
                self.validate_command, "dr1")
            self.assertEqual(len(indexed), 4)
            self.assertTrue(all((artifacts / item["path"]).is_file() for item in indexed))

    def test_missing_parent_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, artifacts = root / "parent", root / "artifacts"
            parent.mkdir()
            with self.assertRaisesRegex(RuntimeError, "evidence_path/evidence_sha256"):
                harness.validate_parent_track_evidence(
                    parent, artifacts, {"tracks": [{"name": "backend"}]}, self.command,
                    self.validate_command, "dr1")

    def test_reordered_event_history_fails_even_when_last_evidence_event_still_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, artifacts = root / "parent", root / "artifacts"
            parent.mkdir()
            events = [
                {"event": "child-state", "phase": "exec", "at": "1"},
                {"event": "child-state", "phase": "merge", "merge_stage": "sync", "at": "2"},
                {"event": "child-state", "phase": "merge", "merge_stage": "confirm", "at": "3"},
            ]
            track = self.build_track(parent, "backend", events)
            cleanup = track["event_history"][len(events):]
            track["event_history"] = [events[1], events[0], events[2], *cleanup]
            with self.assertRaisesRegex(RuntimeError, "不是連續 cleanup 延伸"):
                harness.validate_parent_track_evidence(
                    parent, artifacts, {"tracks": [track]}, self.command,
                    self.validate_command, "dr1")

    def test_event_history_over_fleet_bound_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, artifacts = root / "parent", root / "artifacts"
            parent.mkdir()
            events = [
                {"event": "child-state", "phase": "exec", "at": str(index)}
                for index in range(harness.FLEET_EVENT_HISTORY_LIMIT + 1)
            ]
            track = self.build_track(parent, "backend", events)
            with self.assertRaisesRegex(RuntimeError, "超過 bounded 上限"):
                harness.validate_parent_track_evidence(
                    parent, artifacts, {"tracks": [track]}, self.command,
                    self.validate_command, "dr1")

    def test_validate_command_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent, artifacts = root / "parent", root / "artifacts"
            parent.mkdir()
            track = self.build_track(parent, "backend", [
                {"event": "child-state", "phase": "exec", "at": "1"},
            ])
            evidence_path = Path(track["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["validate_command_sha256"] = "0" * 64
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            track["evidence_sha256"] = harness.sha256_file(evidence_path)
            with self.assertRaisesRegex(RuntimeError, "validate command hash"):
                harness.validate_parent_track_evidence(
                    parent, artifacts, {"tracks": [track]}, self.command,
                    self.validate_command, "dr1")


class TestDryRunReportGate(unittest.TestCase):
    def test_report_requires_semantic_track_and_rollback_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "REPORT.md"
            final_sha = "f" * 40
            state = {"expected_integration_sha": final_sha, "tracks": [
                {"name": "conflict-b", "branch_ref": "refs/heads/loop/run/conflict-b"},
            ]}
            report.write_text(
                "# Parallel Run Report\n## Phase history\n## Merge transaction history\n## Tracks\n"
                "### `conflict-b`\n- branch: `refs/heads/loop/run/conflict-b`\n"
                f"- final: {final_sha}\n- validate rollbacks: 1\n", encoding="utf-8")
            harness.validate_report(report, state, "dr2")
            report.write_text("# Parallel Run Report\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "證據不完整"):
                harness.validate_report(report, state, "dr2")


if __name__ == "__main__":
    unittest.main()

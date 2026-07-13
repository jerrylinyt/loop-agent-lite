"""專案內 Python Dashboard 入口與固定 runtime 路徑回歸測試。"""
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path
from unittest import mock

import dashboard as dashboard_launcher
from engine import dashboard, paths


class TestProjectDashboard(unittest.TestCase):
    def test_root_dashboard_options_are_forwarded(self):
        with mock.patch.object(dashboard, "run_dashboard", return_value=0) as run:
            result = dashboard_launcher.main(["--name", "demo", "--port", "9000", "--read-only"])
        self.assertEqual(result, 0)
        run.assert_called_once_with(name="demo", port=9000, read_only=True)

    def test_runtime_assets_are_inside_engine_package(self):
        package = files("engine")
        for relative in (
            "dashboard.config.shared.json",
            "prompts/plan.md",
            "prompts/exec.md",
            "prompts/external-agent-base.md",
            "prompts/external-agent-goal.md",
            "prompts/external-agent-goal-template.md",
            "prompts/external-agent-plan.md",
            "prompts/external-agent-missing.md",
            "prompts/external-agent-team-template-example.md",
            "ui/index.html",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(package.joinpath(relative).is_file())

    def test_defaults_stay_under_project_root(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(paths, "PROJECT_ROOT", Path(directory)), \
                mock.patch.dict("os.environ", {}, clear=True):
            root = Path(directory).resolve()
            self.assertEqual(paths.default_workspace_root(), root / "workspace")
            self.assertEqual(paths.default_personal_config(), root / "dashboard.config.local.json")
            self.assertEqual(paths.legacy_config_path(), root / "dashboard.config.json")

    def test_explicit_workspace_override_is_kept_for_isolated_runs(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
                "os.environ", {"LOOP_AGENT_WORKSPACE_ROOT": directory}, clear=True):
            self.assertEqual(paths.default_workspace_root(), Path(directory).resolve())


if __name__ == "__main__":
    unittest.main()

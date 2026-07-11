"""可安裝 `loop dashboard` 入口與 package runtime 資源回歸測試。"""
import tempfile
import unittest
from contextlib import redirect_stderr
from importlib.resources import files
from io import StringIO
from pathlib import Path
from unittest import mock

from engine import cli, dashboard, paths


class TestInstalledCli(unittest.TestCase):
    def test_only_dashboard_is_public_and_options_are_forwarded(self):
        with mock.patch.object(dashboard, "run_dashboard", return_value=0) as run:
            result = cli.main(["dashboard", "--name", "demo", "--port", "9000", "--read-only"])
        self.assertEqual(result, 0)
        run.assert_called_once_with(name="demo", port=9000, read_only=True)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["status"])

    def test_runtime_assets_are_inside_engine_package(self):
        package = files("engine")
        for relative in (
            "dashboard.config.shared.json",
            "prompts/plan.md",
            "prompts/exec.md",
            "prompts/external-agent-base.md",
            "prompts/external-agent-goal.md",
            "prompts/external-agent-plan.md",
            "prompts/external-agent-missing.md",
            "prompts/external-agent-default-context.md",
            "prompts/external-agent-team-template-example.md",
            "ui/index.html",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(package.joinpath(relative).is_file())

    def test_wheel_style_defaults_use_user_data_not_package_directory(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(paths, "CHECKOUT_ROOT", None), \
                mock.patch.object(paths, "USER_DATA_ROOT", Path(directory)), \
                mock.patch.dict("os.environ", {}, clear=True):
            root = Path(directory).resolve()
            self.assertEqual(paths.default_workspace_root(), root / "workspace")
            self.assertEqual(paths.default_personal_config(), root / "dashboard.config.local.json")


if __name__ == "__main__":
    unittest.main()

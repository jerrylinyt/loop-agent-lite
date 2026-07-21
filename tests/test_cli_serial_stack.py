"""高階 CLI 對 ordinary Loop serial-stack opt-in 的聚焦契約測試。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import cli


def _state(repo: Path, *, include_flag=True, flag=False):
    config = {
        "repo": str(repo),
        "agent_cmd": "agent --test",
        "validate_cmd": "validator --test",
    }
    if include_flag:
        config["allow_serial_stack"] = flag
    return {"config": config}


class TestCliSerialStackOptIn(unittest.TestCase):
    def test_true_config_replays_low_level_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            config = cli.normalize_runtime_config(
                _state(Path(directory), flag=True))

        argv = cli.config_to_loop_args("serial-opt-in", config)

        self.assertTrue(config["allow_serial_stack"])
        self.assertEqual(argv.count("--allow-serial-stack"), 1)

    def test_wrong_config_types_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            for value in (1, 0, "true", None, [], {}):
                with self.subTest(value=value), self.assertRaisesRegex(
                        ValueError, r"state\.config\.allow_serial_stack 必須是 boolean"):
                    cli.normalize_runtime_config(_state(repo, flag=value))

    def test_legacy_config_defaults_false_without_changing_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            config = cli.normalize_runtime_config(
                _state(Path(directory), include_flag=False))

        argv = cli.config_to_loop_args("legacy", config)

        self.assertIs(config["allow_serial_stack"], False)
        self.assertNotIn("--allow-serial-stack", argv)

    def test_init_parser_forwards_explicit_flag_only_when_requested(self):
        base_argv = [
            "init",
            "--repo", ".",
            "--agent-cmd", "agent --test",
            "--validate-cmd", "validator --test",
        ]
        parser = cli.build_argument_parser()

        with mock.patch.object(cli, "_exec_engine") as execute:
            self.assertEqual(cli.command_init(parser.parse_args(base_argv)), 0)
        self.assertNotIn("--allow-serial-stack", execute.call_args.args[0])

        with mock.patch.object(cli, "_exec_engine") as execute:
            self.assertEqual(cli.command_init(
                parser.parse_args([*base_argv, "--allow-serial-stack"])), 0)
        self.assertEqual(execute.call_args.args[0].count("--allow-serial-stack"), 1)


if __name__ == "__main__":
    unittest.main()

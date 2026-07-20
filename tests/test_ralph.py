#!/usr/bin/env python3
"""engine.ralph 對外純函式回歸測試(stdlib only、無 subprocess)。

涵蓋 RALPH_CONTRACT.md §H 列出的純函式:parse_prd_json / parse_prd_md / load_prd /
build_ralph_argv / resolve_args_template / ARGS_STYLES / project_ralph_block。
監督層 CLI 端到端行為(spawn 真的 fake_ralph.sh)見 tests/test_ralph_integration.py。

跑法:  python3 -m unittest tests.test_ralph      # 或  python3 tests/test_ralph.py
"""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from engine import loop as loop_mod  # noqa: E402
from engine import ralph as R  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _make_repo(root: Path) -> Path:
    """最小 git repo:單一 commit,供 project_ralph_block 的 git 投影測試用。"""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "ralph-test@example.invalid")
    _git(repo, "config", "user.name", "Ralph Test")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return repo


class TestParsePrdJson(unittest.TestCase):
    """parse_prd_json(text) — snarktank 格式(dict.userStories 或純 story 陣列)。"""

    def test_valid_snarktank_shape_reports_project_branch_and_counts(self):
        text = json.dumps({
            "project": "Acme",
            "branchName": "ralph/acme-fix",
            "userStories": [
                {"id": "US-001", "title": "Story A", "passes": True, "priority": 1},
                {"id": "US-002", "title": "Story B", "passes": False, "priority": 2},
            ],
        })
        result = R.parse_prd_json(text)
        self.assertEqual(result["prd_format"], "json")
        self.assertEqual(result["project"], "Acme")
        self.assertEqual(result["branch_name"], "ralph/acme-fix")
        self.assertEqual(result["stories_total"], 2)
        self.assertEqual(result["stories_done"], 1)
        self.assertEqual([s["id"] for s in result["stories"]], ["US-001", "US-002"])
        self.assertIs(result["stories"][0]["passes"], True)
        self.assertIs(result["stories"][1]["passes"], False)

    def test_top_level_list_of_stories_is_accepted(self):
        text = json.dumps([
            {"id": "A", "title": "one", "passes": True},
            {"id": "B", "title": "two", "passes": True},
            {"id": "C", "title": "three", "passes": False},
        ])
        result = R.parse_prd_json(text)
        self.assertEqual(result["project"], "")
        self.assertEqual(result["branch_name"], "")
        self.assertEqual(result["stories_total"], 3)
        self.assertEqual(result["stories_done"], 2)

    def test_missing_user_stories_raises(self):
        with self.assertRaises(R.PrdParseError):
            R.parse_prd_json(json.dumps({"project": "Acme"}))
        with self.assertRaises(R.PrdParseError):
            R.parse_prd_json(json.dumps({"userStories": "not-a-list"}))
        with self.assertRaises(R.PrdParseError):
            R.parse_prd_json(json.dumps({"userStories": None}))

    def test_invalid_json_raises(self):
        with self.assertRaises(R.PrdParseError):
            R.parse_prd_json("{not valid json")
        with self.assertRaises(R.PrdParseError):
            R.parse_prd_json("")

    def test_passes_field_is_coerced_to_bool_by_truthiness(self):
        text = json.dumps({"userStories": [
            {"id": "T", "title": "truthy-int", "passes": 1},
            {"id": "F", "title": "falsy-int", "passes": 0},
            {"id": "S", "title": "truthy-str", "passes": "yes"},
            {"id": "E", "title": "falsy-str", "passes": ""},
            {"id": "M", "title": "missing-key"},
        ]})
        result = R.parse_prd_json(text)
        by_id = {s["id"]: s["passes"] for s in result["stories"]}
        self.assertEqual(by_id, {"T": True, "F": False, "S": True, "E": False, "M": False})
        for value in by_id.values():
            self.assertIsInstance(value, bool)
        self.assertEqual(result["stories_done"], 2)  # 只有 T 和 S 是 truthy

    def test_story_display_cap_limits_list_but_totals_count_everything(self):
        cap = R.STORY_DISPLAY_CAP
        stories = [{"id": f"US-{i:04d}", "title": f"story {i}", "passes": False}
                   for i in range(cap)]
        stories += [{"id": f"US-{i:04d}", "title": f"story {i}", "passes": True}
                    for i in range(cap, cap + 50)]
        result = R.parse_prd_json(json.dumps({"userStories": stories}))
        self.assertEqual(result["stories_total"], cap + 50)
        self.assertEqual(result["stories_done"], 50, "完成數必須算全部,不受顯示上限影響")
        self.assertEqual(len(result["stories"]), cap, "顯示清單必須截斷在 STORY_DISPLAY_CAP")
        self.assertTrue(all(not s["passes"] for s in result["stories"]),
                        "被截斷掉的都是後面 50 筆 passes=True,顯示清單應全部是前段 passes=False")


class TestParsePrdMd(unittest.TestCase):
    """parse_prd_md(text) — checkbox 清單(`- [ ]` / `- [x]`)。"""

    def test_checkbox_parsing_marks_done_and_pending(self):
        text = (
            "# Some Project\n\n"
            "- [x] done story one\n"
            "- [ ] pending story two\n"
            "- [X] done story three (uppercase mark)\n"
            "* [ ] pending story four (asterisk bullet)\n"
        )
        result = R.parse_prd_md(text)
        self.assertEqual(result["prd_format"], "md")
        self.assertEqual(result["stories_total"], 4)
        self.assertEqual(result["stories_done"], 2)
        self.assertEqual(
            [s["passes"] for s in result["stories"]],
            [True, False, True, False],
        )
        self.assertEqual(result["stories"][0]["id"], "US-001")
        self.assertEqual(result["stories"][0]["title"], "done story one")
        self.assertEqual([s["priority"] for s in result["stories"]], [1, 2, 3, 4])

    def test_heading_becomes_project_name(self):
        result = R.parse_prd_md("# My Great Project\n\n- [ ] only story\n")
        self.assertEqual(result["project"], "My Great Project")

    def test_empty_text_raises(self):
        with self.assertRaises(R.PrdParseError):
            R.parse_prd_md("")

    def test_text_without_checkboxes_raises(self):
        with self.assertRaises(R.PrdParseError):
            R.parse_prd_md("# Project With No Stories\n\nJust prose, no checkboxes.\n")


class TestLoadPrd(unittest.TestCase):
    """load_prd(ralph_dir, prd_path) — 安全讀取＋偵測格式;任何錯誤回 prd_error 不丟例外。"""

    def test_reads_json_fixture(self):
        result = R.load_prd(FIXTURES, "prd.json")
        self.assertIsNone(result["prd_error"])
        self.assertEqual(result["prd_format"], "json")
        self.assertEqual(result["prd_path"], "prd.json")
        self.assertEqual(result["project"], "FixtureApp")
        self.assertEqual(result["branch_name"], "ralph/fixture")
        self.assertEqual(result["stories_total"], 2)
        self.assertEqual(result["stories_done"], 0)

    def test_reads_md_fixture(self):
        result = R.load_prd(FIXTURES, "prd.md")
        self.assertIsNone(result["prd_error"])
        self.assertEqual(result["prd_format"], "md")
        self.assertEqual(result["project"], "Fixture Markdown App")
        self.assertEqual(result["branch_name"], "ralph/fixture-md")
        self.assertEqual(result["stories_total"], 3)
        self.assertEqual(result["stories_done"], 2)

    def test_missing_file_sets_prd_error_without_raising(self):
        result = R.load_prd(FIXTURES, "does-not-exist.json")
        self.assertIsNotNone(result["prd_error"])
        self.assertEqual(result["stories"], [])
        self.assertEqual(result["stories_total"], 0)
        self.assertEqual(result["stories_done"], 0)

    def test_path_traversal_is_rejected_via_prd_error(self):
        result = R.load_prd(FIXTURES, "../outside.json")
        self.assertIsNotNone(result["prd_error"])
        self.assertEqual(result["stories"], [])

    def test_absolute_path_is_rejected_via_prd_error(self):
        result = R.load_prd(FIXTURES, "/etc/passwd")
        self.assertIsNotNone(result["prd_error"])
        self.assertEqual(result["stories"], [])

    def test_format_is_detected_by_extension(self):
        self.assertEqual(R.load_prd(FIXTURES, "prd.md")["prd_format"], "md")
        self.assertEqual(R.load_prd(FIXTURES, "PRD.MD")["prd_format"], "md")
        self.assertEqual(R.load_prd(FIXTURES, "prd.json")["prd_format"], "json")
        self.assertEqual(R.load_prd(FIXTURES, "no-extension")["prd_format"], "json")


class TestBuildRalphArgv(unittest.TestCase):
    """build_ralph_argv(base_cmd, args_template, *, iterations, tool, model, prd_path)。"""

    def test_positional_style_appends_iterations_tool_model(self):
        argv = R.build_ralph_argv(
            "sh /x/ralph.sh", R.ARGS_STYLES["positional"],
            iterations=5, tool="opencode", model="m")
        self.assertEqual(argv, ["sh", "/x/ralph.sh", "5", "opencode", "m"])

    def test_empty_model_token_is_dropped_entirely(self):
        argv = R.build_ralph_argv(
            "sh /x/ralph.sh", R.ARGS_STYLES["positional"],
            iterations=5, tool="opencode", model="")
        self.assertEqual(argv, ["sh", "/x/ralph.sh", "5", "opencode"])
        self.assertNotIn("", argv)

    def test_snarktank_style_uses_tool_flag_then_iterations(self):
        argv = R.build_ralph_argv(
            "sh /x/ralph.sh", R.ARGS_STYLES["snarktank"],
            iterations=5, tool="claude", model="")
        self.assertEqual(argv[:2], ["sh", "/x/ralph.sh"])
        self.assertEqual(argv[2:], ["--tool", "claude", "5"])

    def test_embedded_placeholder_is_substituted_within_token(self):
        argv = R.build_ralph_argv(
            "sh /x/ralph.sh", ["--model={model}", "--prd={prd}"],
            iterations=1, tool="claude", model="gpt5", prd_path="prd.json")
        self.assertEqual(argv[2:], ["--model=gpt5", "--prd=prd.json"])

    def test_base_command_is_shlex_split(self):
        argv = R.build_ralph_argv("sh /x/ralph.sh", [], iterations=1, tool="t", model="")
        self.assertEqual(argv, ["sh", "/x/ralph.sh"])


class TestResolveArgsTemplate(unittest.TestCase):
    """resolve_args_template(style, explicit=None)。"""

    def test_positional_and_snarktank_return_independent_copies(self):
        positional = R.resolve_args_template("positional")
        self.assertEqual(positional, ["{iterations}", "{tool}", "{model}"])
        positional.append("mutated")
        self.assertEqual(
            R.ARGS_STYLES["positional"], ["{iterations}", "{tool}", "{model}"],
            "resolve_args_template 不得回傳可改到 ARGS_STYLES 本體的參照")

        snarktank = R.resolve_args_template("snarktank")
        self.assertEqual(snarktank, ["--tool", "{tool}", "{iterations}"])

    def test_custom_requires_explicit_string_list(self):
        explicit = ["--foo", "{tool}"]
        self.assertEqual(R.resolve_args_template("custom", explicit), explicit)
        with self.assertRaises(ValueError):
            R.resolve_args_template("custom", None)
        with self.assertRaises(ValueError):
            R.resolve_args_template("custom", "not-a-list")
        with self.assertRaises(ValueError):
            R.resolve_args_template("custom", ["ok", 5])

    def test_unknown_style_raises(self):
        with self.assertRaises(ValueError):
            R.resolve_args_template("bogus-style")


class TestProjectRalphBlock(unittest.TestCase):
    """project_ralph_block(...) — state.json 的 `ralph` 區塊(契約 §A)。"""

    def test_block_has_contract_fields_and_composes_into_valid_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = _make_repo(root)
            base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

            ralph_dir = root / "ralph_dir"
            ralph_dir.mkdir()
            (ralph_dir / "prd.json").write_text(
                (FIXTURES / "prd.json").read_text(encoding="utf-8"), encoding="utf-8")
            progress_text = "# Ralph Progress Log\nStarted: t\n---\n"
            (ralph_dir / "progress.txt").write_text(progress_text, encoding="utf-8")

            # 讓 repo 在 base_sha 之後前進兩個 commit,驅動 commit_count/last_commit。
            (repo / "a.txt").write_text("a\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "feat: a")
            (repo / "b.txt").write_text("b\n", encoding="utf-8")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-qm", "feat: b")
            head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

            block = R.project_ralph_block(
                repo, ralph_dir, "prd.json", base_sha,
                iteration=3, max_iterations=10, sentinel_complete=False,
                stalled=False, exit_code=None, exit_reason=None)

            # ---- §A 逐欄位型別檢查 ----
            self.assertEqual(block["prd_format"], "json")
            self.assertEqual(block["prd_path"], "prd.json")
            self.assertEqual(block["project"], "FixtureApp")
            self.assertEqual(block["branch_name"], "ralph/fixture")
            self.assertIsInstance(block["stories"], list)
            self.assertEqual(block["stories_total"], 2)
            self.assertEqual(block["stories_done"], 0)
            self.assertEqual(block["iteration"], 3)
            self.assertIsInstance(block["iteration"], int)
            self.assertEqual(block["max_iterations"], 10)
            self.assertEqual(block["base_sha"], base_sha)
            self.assertEqual(block["head_sha"], head_sha)
            self.assertEqual(block["commit_count"], 2)
            self.assertEqual(block["last_commit"], "feat: b")
            self.assertEqual(block["progress_bytes"], len(progress_text.encode("utf-8")))
            self.assertIs(block["sentinel_complete"], False)
            self.assertIs(block["stalled"], False)
            self.assertIsNone(block["exit_code"])
            self.assertIsNone(block["exit_reason"])
            self.assertIsNone(block["prd_error"])
            datetime.fromisoformat(block["updated_at"])  # 必須是合法 ISO 時間字串

            # project_ralph_block 只投影 ralph 專屬進度;不得混入 coordinator 收斂欄位。
            for field in ("flag", "done_count", "plan", "completed", "red_streak"):
                self.assertNotIn(field, block)

            # ---- 組成完整 ralph runner state.json,必須通過既有的 validate_state_shape ----
            state = {
                "runner": "ralph",
                "phase": "exec",
                "loop": {"pid": 4242, "session_id": "test-session",
                         "started_at": "2026-07-20T00:00:00"},
                "repo_binding": str(repo),
                "config": {
                    "runner": "ralph",
                    "repo": str(repo),
                    "ralph_cmd": "sh /x/ralph.sh",
                    "ralph_dir": str(ralph_dir),
                    "iterations": 10,
                    "tool": "claude",
                    "model": "",
                    "args_template": list(R.ARGS_STYLES["positional"]),
                    "prd_path": "prd.json",
                    "notify_cmd": "",
                },
                "ralph": block,
            }
            validated = loop_mod.validate_state_shape(state, "state.json")
            self.assertIs(validated, state)

            for field in ("flag", "done_count", "plan", "completed", "red_streak"):
                self.assertNotIn(field, state)


if __name__ == "__main__":
    unittest.main()

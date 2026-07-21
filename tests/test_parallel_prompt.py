"""Exec prompt stays unchanged for ordinary loops and is conditional for workers."""

import re
import unittest
from pathlib import Path

from engine import loop
from engine import parallel_contract as contract


EXEC_PROMPT = Path(loop.__file__).resolve().parent / "prompts" / "exec.md"


def mapping(*, sync="", report="/runtime/python -m engine.work issue"):
    return {
        "GOAL": "goal",
        "PLAN_DOC": "(none)",
        "TASK_ID": "task-2",
        "TASK_TEXT": "implement the task",
        "TASK_REF": "(none)",
        "TASK_LIST": "[→] task-2: implement the task",
        "DONE_CMD": "/runtime/python -m engine.work done task-2",
        "ISSUE_CMD": report,
        "VALIDATE_CMD": "python -m unittest",
        "SYNC_INTEGRATION": sync,
        "NOTES": "(none)",
    }


class TestConditionalExecPrompt(unittest.TestCase):
    def test_ordinary_prompt_has_no_parallel_sync_section_or_placeholder(self):
        prompt = loop.build_prompt(EXEC_PROMPT, mapping())

        self.assertNotIn("同步整合基線", prompt)
        self.assertNotIn("parallel", prompt.lower())
        self.assertNotRegex(prompt, r"<<[A-Z][A-Z0-9_]*>>")
        self.assertIn("engine.work issue", prompt)

    def test_worker_sync_is_between_cleanup_and_completion_and_uses_block(self):
        block = "/runtime/python -m engine.work block --reason"
        sync = contract.managed_sync_instructions(
            contract.integration_ref_for("a1b2c3d4"), block)
        prompt = loop.build_prompt(EXEC_PROMPT, mapping(sync=sync, report=block))

        self.assertLess(prompt.index("收拾現場"), prompt.index("同步整合基線"))
        self.assertLess(prompt.index("同步整合基線"), prompt.index("判斷本任務是否已完成"))
        self.assertIn("engine.work block --reason", prompt)
        self.assertNotIn("engine.work issue", prompt)
        self.assertNotRegex(prompt, r"<<[A-Z][A-Z0-9_]*>>")

    def test_build_prompt_fails_when_a_runtime_placeholder_is_unresolved(self):
        values = mapping()
        values.pop("SYNC_INTEGRATION")
        with self.assertRaisesRegex(ValueError, "placeholder"):
            loop.build_prompt(EXEC_PROMPT, values)

    def test_placeholder_looking_runtime_text_is_never_reinterpreted(self):
        values = mapping(sync="managed sync text")
        values["GOAL"] = "literal <<SYNC_INTEGRATION>> and <<EXAMPLE_TOKEN>>"
        values["TASK_TEXT"] = "keep <<ISSUE_CMD>> literal"

        prompt = loop.build_prompt(EXEC_PROMPT, values)

        self.assertIn("literal <<SYNC_INTEGRATION>> and <<EXAMPLE_TOKEN>>", prompt)
        self.assertIn("keep <<ISSUE_CMD>> literal", prompt)
        self.assertEqual(prompt.count("managed sync text"), 1)


if __name__ == "__main__":
    unittest.main()

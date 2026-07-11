"""Prompt 模板目錄與團隊/個人設定邊界的回歸測試。"""

import json
import tempfile
import unittest
from pathlib import Path

import dashboard as D
import prompt_templates as P


def team_template(template_id="team-flow", **overrides):
    value = {
        "id": template_id,
        "label": "團隊流程分析",
        "category": "團隊",
        "description": "追蹤團隊專屬流程",
        "requirement_placeholder": "請貼上要分析的流程",
        "instructions": "- 盤點狀態。\n- 追蹤資料流。",
    }
    value.update(overrides)
    return value


class TestPromptTemplateCatalog(unittest.TestCase):
    def test_builtin_catalog_contains_existing_and_analysis_templates(self):
        templates, warnings = P.prompt_template_projection({})
        ids = {item["id"] for item in templates}
        self.assertFalse(warnings)
        self.assertTrue({
            "java-generic", "ejb-springboot-migration", "jsp-react-migration",
            "project-logic-analysis", "code-logic-analysis",
        }.issubset(ids))
        self.assertTrue(all(item["source"] == "builtin" for item in templates))

    def test_valid_team_template_is_appended_without_replacing_contracts(self):
        templates, warnings = P.prompt_template_projection({
            "prompt_templates": [team_template()]
        })
        self.assertFalse(warnings)
        added = templates[-1]
        self.assertEqual(added["id"], "team-flow")
        self.assertEqual(added["source"], "team")
        self.assertEqual(added["instructions"], "- 盤點狀態。\n- 追蹤資料流。")

    def test_invalid_and_duplicate_team_templates_are_skipped_with_warnings(self):
        templates, warnings = P.prompt_template_projection({
            "prompt_templates": [
                team_template("new-feature"),
                team_template("Upper Case"),
                team_template("unknown-field", typo="value"),
                {"id": "missing-instructions", "label": "缺少內容"},
            ]
        })
        self.assertEqual(len(templates), len(P.BUILTIN_PROMPT_TEMPLATES))
        self.assertEqual(len(warnings), 4)
        self.assertTrue(any("重複" in warning for warning in warnings))
        self.assertTrue(any("小寫英數" in warning for warning in warnings))
        self.assertTrue(any("未知欄位" in warning for warning in warnings))
        self.assertTrue(any("instructions" in warning for warning in warnings))

    def test_team_template_count_is_bounded(self):
        values = [team_template(f"team-{index}") for index in range(P.MAX_TEAM_PROMPT_TEMPLATES + 3)]
        templates, warnings = P.prompt_template_projection({"prompt_templates": values})
        team = [item for item in templates if item["source"] == "team"]
        self.assertEqual(len(team), P.MAX_TEAM_PROMPT_TEMPLATES)
        self.assertTrue(any("最多" in warning for warning in warnings))

    def test_config_projection_exposes_catalog_and_warning(self):
        projected = D.config_projection({
            "repo_roots": [],
            "prompt_templates": "not-an-array",
        })
        self.assertEqual(len(projected["prompt_templates"]), len(P.BUILTIN_PROMPT_TEMPLATES))
        self.assertTrue(projected["prompt_template_warnings"])


class TestTeamTemplateConfigBoundary(unittest.TestCase):
    def test_personal_config_cannot_override_shared_prompt_templates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "dashboard.config.shared.json"
            personal_path = root / "dashboard.config.local.json"
            legacy_path = root / "dashboard.config.json"
            project_path.write_text(json.dumps({
                "prompt_templates": [team_template("shared-template")],
                "repo_roots": [],
            }), encoding="utf-8")
            personal_path.write_text(json.dumps({
                "prompt_templates": [team_template("personal-template")],
                "repo_roots": [],
            }), encoding="utf-8")
            old = (
                D.CONFIG_OVERRIDE, D.PROJECT_CONFIG_PATH, D.PERSONAL_CONFIG_PATH,
                D.LEGACY_CONFIG_PATH, D.CONFIG_PATH,
            )
            try:
                D.CONFIG_OVERRIDE = None
                D.PROJECT_CONFIG_PATH = project_path
                D.PERSONAL_CONFIG_PATH = personal_path
                D.LEGACY_CONFIG_PATH = legacy_path
                D.CONFIG_PATH = personal_path
                loaded = D.load_config()
            finally:
                (
                    D.CONFIG_OVERRIDE, D.PROJECT_CONFIG_PATH, D.PERSONAL_CONFIG_PATH,
                    D.LEGACY_CONFIG_PATH, D.CONFIG_PATH,
                ) = old
            self.assertEqual(loaded["prompt_templates"][0]["id"], "shared-template")


if __name__ == "__main__":
    unittest.main()

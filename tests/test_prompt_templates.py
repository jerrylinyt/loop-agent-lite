"""Prompt 模板目錄與團隊/個人設定邊界的回歸測試。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import dashboard as D
from engine import prompt_templates as P


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


class TestPromptTemplateResources(unittest.TestCase):
    def tearDown(self):
        P.prompt_template_bundle.cache_clear()

    def test_packaged_bundle_is_versioned_complete_and_contract_last(self):
        P.prompt_template_bundle.cache_clear()
        bundle, error = P.prompt_template_bundle()
        self.assertIsNone(error)
        self.assertEqual(bundle["schema_version"], P.PROMPT_TEMPLATE_BUNDLE_SCHEMA_VERSION)
        self.assertEqual(
            set(bundle),
            {
                "schema_version", "base", "goal", "plan", "missing_requirement",
                "default_context", "team_template_example",
            },
        )
        self.assertTrue(bundle["base"].endswith("<<MODE_CONTRACT>>"))
        self.assertIn("最終輸出契約：goal.md", bundle["goal"])
        self.assertIn("最終輸出契約：plan.json", bundle["plan"])

    def test_invalid_resource_makes_entire_bundle_unavailable(self):
        originals = {
            filename: P._read_prompt_resource(filename)
            for filename, _, _ in P.PROMPT_RESOURCE_SPECS.values()
        }
        base_name = P.PROMPT_RESOURCE_SPECS["base"][0]
        cases = {
            "duplicate": (
                {base_name: originals[base_name].replace(
                    "<<OUTPUT_NAME>>", "<<OUTPUT_NAME>><<OUTPUT_NAME>>", 1
                )},
                "次數錯誤",
            ),
            "unknown": ({base_name: originals[base_name] + "\n<<UNKNOWN>>"}, "未知"),
            "malformed": (
                {base_name: originals[base_name].replace(
                    "<<OUTPUT_NAME>>", "<<output_name>>", 1
                )},
                "marker",
            ),
            "contract-not-last": ({base_name: originals[base_name] + "\n後置文字"}, "結尾"),
        }
        for label, (overrides, expected) in cases.items():
            with self.subTest(case=label), mock.patch.object(
                P,
                "_read_prompt_resource",
                side_effect=lambda filename, values={**originals, **overrides}: values[filename],
            ):
                P.prompt_template_bundle.cache_clear()
                bundle, error = P.prompt_template_bundle()
                self.assertIsNone(bundle)
                self.assertIn(expected, error)

    def test_unreadable_resource_makes_entire_bundle_unavailable(self):
        original = P._read_prompt_resource

        def fail_plan(filename):
            if filename == P.PROMPT_RESOURCE_SPECS["plan"][0]:
                raise OSError("missing")
            return original(filename)

        with mock.patch.object(P, "_read_prompt_resource", side_effect=fail_plan):
            P.prompt_template_bundle.cache_clear()
            bundle, error = P.prompt_template_bundle()
        self.assertIsNone(bundle)
        self.assertIn("無法讀取", error)


class TestPromptTemplateCatalog(unittest.TestCase):
    def test_builtin_catalog_contains_existing_and_analysis_templates(self):
        templates, warnings = P.prompt_template_projection({})
        ids = {item["id"] for item in templates}
        self.assertFalse(warnings)
        self.assertTrue({
            "java-generic", "ejb-springboot-migration", "jsp-react-migration",
            "project-logic-analysis", "code-logic-analysis",
            "change-impact-analysis", "db-migration", "schema-data-rollout",
            "dependency-upgrade", "k8s-deployment-config",
            "incident-root-cause", "security-scan-remediation",
        }.issubset(ids))
        self.assertTrue(all(item["source"] == "builtin" for item in templates))

    def test_builtin_catalog_has_unique_complete_prompt_guidance(self):
        templates, warnings = P.prompt_template_projection({})
        self.assertFalse(warnings)
        ids = [item["id"] for item in templates]
        self.assertEqual(len(ids), len(set(ids)))
        for item in templates:
            with self.subTest(template=item["id"]):
                self.assertRegex(item["id"], P.TEMPLATE_ID_RE)
                self.assertTrue(item["label"].strip())
                self.assertTrue(item["category"].strip())
                self.assertTrue(item["description"].strip())
                self.assertTrue(item["requirement_placeholder"].strip())
                self.assertGreaterEqual(item["instructions"].count("- "), 4)

    def test_high_risk_migration_templates_require_versioned_evidence_and_real_validation(self):
        catalog = {item["id"]: item for item in P.BUILTIN_PROMPT_TEMPLATES}
        self.assertIn("來源／目標資料庫", catalog["db-migration"]["instructions"])
        self.assertIn("分別在來源與目標資料庫執行", catalog["db-migration"]["instructions"])
        self.assertIn("autoconfiguration", catalog["dependency-upgrade"]["instructions"])
        self.assertIn("具備證據後才能判 N/A", catalog["dependency-upgrade"]["instructions"])
        self.assertIn("pure YAML、Kustomize、Helm 三種", catalog["k8s-deployment-config"]["instructions"])
        self.assertIn("不得把相關性寫成因果", catalog["incident-root-cause"]["instructions"])
        self.assertIn("不得靜默忽略", catalog["security-scan-remediation"]["instructions"])
        self.assertIn("重跑掃描並比對前後結果", catalog["security-scan-remediation"]["instructions"])
        self.assertIn("helm lint <chart-dir> -f <values-file>", catalog["k8s-deployment-config"]["instructions"])
        self.assertIn("helm template <release> <chart-dir> -f <values-file>", catalog["k8s-deployment-config"]["instructions"])

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
        self.assertEqual(projected["prompt_template_bundle"]["schema_version"], 1)
        self.assertIsNone(projected["prompt_template_bundle_error"])

    def test_config_projection_keeps_dashboard_usable_when_fixed_bundle_fails(self):
        with mock.patch.object(D, "prompt_template_bundle", return_value=(None, "resource broken")):
            projected = D.config_projection({"repo_roots": []})
        self.assertIsNone(projected["prompt_template_bundle"])
        self.assertEqual(projected["prompt_template_bundle_error"], "resource broken")
        self.assertEqual(len(projected["prompt_templates"]), len(P.BUILTIN_PROMPT_TEMPLATES))
        self.assertIn("repos", projected)


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

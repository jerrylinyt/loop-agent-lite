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
            "change-impact-analysis", "db-migration", "oracle-mariadb-migration",
            "schema-data-rollout", "dependency-upgrade", "k8s-deployment-config",
            "incident-root-cause", "security-scan-remediation",
            "java-test-completion", "react-playwright-testing",
            "characterization-test", "api-contract-testing",
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

    def test_every_projected_template_keeps_discovery_targeted_and_agent_first(self):
        templates, warnings = P.prompt_template_projection({
            "prompt_templates": [team_template()]
        })
        self.assertFalse(warnings)
        for item in templates:
            with self.subTest(template=item["id"]):
                self.assertTrue(item["instructions"].startswith(P.LIMITED_DISCOVERY_PREFIX))
                self.assertIn("不授權全 repo 列檔、廣域搜巡", item["instructions"])
        new_feature = next(item for item in templates if item["id"] == "new-feature")
        self.assertIn("不要求固定六層矩陣", new_feature["instructions"])
        self.assertIn("不為格式增加 ID", new_feature["instructions"])

    def test_high_risk_migration_templates_require_versioned_evidence_and_real_validation(self):
        catalog = {item["id"]: item for item in P.BUILTIN_PROMPT_TEMPLATES}
        self.assertIn("來源／目標資料庫", catalog["db-migration"]["instructions"])
        self.assertIn("分別在來源與目標資料庫執行", catalog["db-migration"]["instructions"])
        self.assertIn("不得以「MySQL 相容」概括", catalog["oracle-mariadb-migration"]["instructions"])
        self.assertIn("空字串與 NULL", catalog["oracle-mariadb-migration"]["instructions"])
        self.assertIn(
            "分別在 Oracle 與 MariaDB 執行", catalog["oracle-mariadb-migration"]["instructions"]
        )
        self.assertIn("CONNECT BY", catalog["oracle-mariadb-migration"]["instructions"])
        self.assertIn(
            "sequence.NEXTVAL／CURRVAL", catalog["oracle-mariadb-migration"]["instructions"]
        )
        self.assertIn("LAST_INSERT_ID()", catalog["oracle-mariadb-migration"]["instructions"])
        self.assertIn(
            "procedure／function／trigger／event", catalog["oracle-mariadb-migration"]["instructions"]
        )
        self.assertIn(
            "human gate，不自行替團隊決策", catalog["oracle-mariadb-migration"]["instructions"]
        )
        self.assertIn("不得交付恆綠測試", catalog["java-test-completion"]["instructions"])
        self.assertIn(
            "規劃成前置任務並由 agent", catalog["java-test-completion"]["instructions"]
        )
        self.assertIn(
            "避免測試只證明 mock 本身", catalog["java-test-completion"]["instructions"]
        )
        self.assertIn("route／fulfill", catalog["react-playwright-testing"]["instructions"])
        self.assertIn("不得自創欄位", catalog["react-playwright-testing"]["instructions"])
        self.assertIn("禁止固定 sleep", catalog["react-playwright-testing"]["instructions"])
        self.assertIn(
            "只記錄現況、不判斷對錯", catalog["characterization-test"]["instructions"]
        )
        self.assertIn(
            "疑似 bug 另列清單交人裁決", catalog["characterization-test"]["instructions"]
        )
        self.assertIn("不得順手修", catalog["characterization-test"]["instructions"])
        self.assertIn(
            "優先斷言輸出與持久化結果", catalog["characterization-test"]["instructions"]
        )
        self.assertIn(
            "不斷言內部呼叫順序或私有狀態", catalog["characterization-test"]["instructions"]
        )
        self.assertIn(
            "fail／escalate 不得靜默放行", catalog["api-contract-testing"]["instructions"]
        )
        self.assertIn("請求側與回應側", catalog["api-contract-testing"]["instructions"])
        self.assertIn("結構化比對", catalog["api-contract-testing"]["instructions"])
        self.assertIn(
            "不得整包字串 snapshot", catalog["api-contract-testing"]["instructions"]
        )
        self.assertIn(
            "新增欄位是否破壞消費端", catalog["api-contract-testing"]["instructions"]
        )
        self.assertIn(
            "不為未涉及情境補固定矩陣或空列",
            catalog["api-contract-testing"]["instructions"],
        )
        self.assertIn(
            "只在整體取證邊界摘要一次",
            catalog["api-contract-testing"]["instructions"],
        )
        self.assertIn("autoconfiguration", catalog["dependency-upgrade"]["instructions"])
        self.assertIn(
            "不為每個未命中候選逐項造空列",
            catalog["dependency-upgrade"]["instructions"],
        )
        self.assertIn(
            "由 agent 依需求與現有 toolchain 選擇最小可維護方案",
            catalog["k8s-deployment-config"]["instructions"],
        )
        self.assertIn("不得把相關性寫成因果", catalog["incident-root-cause"]["instructions"])
        self.assertIn("不得靜默忽略", catalog["security-scan-remediation"]["instructions"])
        self.assertIn("重跑掃描並比對前後結果", catalog["security-scan-remediation"]["instructions"])
        self.assertIn("helm lint <chart-dir> -f <values-file>", catalog["k8s-deployment-config"]["instructions"])
        self.assertIn("helm template <release> <chart-dir> -f <values-file>", catalog["k8s-deployment-config"]["instructions"])

    def test_specialized_templates_keep_normal_unknowns_agent_owned_and_avoid_fixed_na_rows(self):
        catalog = {item["id"]: item for item in P.BUILTIN_PROMPT_TEMPLATES}

        java = catalog["java-test-completion"]["instructions"]
        self.assertIn("規劃成前置任務並由 agent", java)
        self.assertIn("只有建立設施會改變需求意圖", java)
        self.assertNotIn("先列前置任務或 human gate", java)

        data_flow = catalog["api-data-flow-analysis"]["instructions"]
        self.assertIn("沒有正式契約時", data_flow)
        self.assertIn("由 agent 建立符合需求與 repo 慣例的最小契約", data_flow)
        self.assertNotIn("authoritative contract 不存在", data_flow)

        k8s = catalog["k8s-deployment-config"]["instructions"]
        self.assertIn("若都無訊號，由 agent", k8s)
        self.assertIn("只有來源衝突牽涉外部平台政策", k8s)
        self.assertNotIn("三種方案並列 human gate", k8s)

        forbidden_mechanical_requirements = (
            "沒有則以證據標 N/A",
            "具備證據後才能判 N/A",
            "N/A 項附證據",
            "不適用者附證據標 N/A",
            "清單每一項都要有結論",
            "端點×情境矩陣全數",
            "建立相容矩陣：",
            "機器比對至少包含",
        )
        for item in P.BUILTIN_PROMPT_TEMPLATES:
            for phrase in forbidden_mechanical_requirements:
                with self.subTest(template=item["id"], phrase=phrase):
                    self.assertNotIn(phrase, item["instructions"])

        for template_id in (
            "api-contract-testing", "change-impact-analysis", "java-generic",
            "db-migration", "oracle-mariadb-migration", "dependency-upgrade",
            "k8s-deployment-config",
        ):
            with self.subTest(template=template_id):
                instructions = catalog[template_id]["instructions"]
                self.assertTrue(
                    "取證邊界" in instructions
                    or "停止邊界" in instructions
                    or "未知邊界" in instructions,
                    "移除機械空列後仍必須保留 bounded evidence/unknown 摘要",
                )

    def test_valid_team_template_is_appended_without_replacing_contracts(self):
        templates, warnings = P.prompt_template_projection({
            "prompt_templates": [team_template()]
        })
        self.assertFalse(warnings)
        added = templates[-1]
        self.assertEqual(added["id"], "team-flow")
        self.assertEqual(added["source"], "team")
        self.assertEqual(
            added["instructions"],
            f"{P.LIMITED_DISCOVERY_PREFIX}\n- 盤點狀態。\n- 追蹤資料流。",
        )

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

"""Runtime checks: model settings are validated at start-up and the template supplies them."""
import ast
import os
import unittest
from unittest.mock import patch

from checks_support import MODEL_ENV, ROOT, load


class ModelSettingsChecks(unittest.TestCase):
    """Model settings are validated once, at start-up instead of at import, and the template supplies them (RTGW-8)."""

    def test_model_settings_are_read_once_on_first_use_and_each_bad_value_is_named(self):
        """Check importing model_config reads no setting, and settings() names every missing or invalid one."""
        with patch.dict(os.environ, {}, clear=True):
            config = load("checked_model_config", "backend/runtime/utils/model_config.py")
        env = {**MODEL_ENV, "DEPLOYED_GuardrailId": "gr-fixture", "DEPLOYED_GuardrailVersion": "1"}
        for change, named in (({"COMM_MODEL_ID": ""}, "COMM_MODEL_ID is not set"),
                              # The simulation model no longer falls back to the detection model.
                              ({"SIMULATION_MODEL_ID": " "}, "SIMULATION_MODEL_ID is not set"),
                              ({"MODEL_TEMPERATURE": "warm"}, "MODEL_TEMPERATURE must be a number from 0 to 1"),
                              ({"MODEL_TEMPERATURE": "1.5"}, "MODEL_TEMPERATURE must be a number from 0 to 1"),
                              ({"MODEL_TEMPERATURE": ""}, "MODEL_TEMPERATURE is not set")):
            config.settings.cache_clear()
            with self.subTest(change=change), patch.dict(os.environ, {**env, **change}, clear=True), \
                 self.assertRaisesRegex(RuntimeError, named):
                config.settings()
        # One start-up names every problem at once.
        config.settings.cache_clear()
        with patch.dict(os.environ, {"AGENTCORE_RUNTIME": "true"}, clear=True), \
             self.assertRaises(RuntimeError) as raised:
            config.settings()
        for name in (*config.MODEL_ID_SETTINGS.values(), "MODEL_TEMPERATURE", "DEPLOYED_GuardrailId"):
            self.assertIn(name, str(raised.exception))
        # Read once: a later change to the environment does not change the settings in use.
        config.settings.cache_clear()
        with patch.dict(os.environ, env, clear=True):
            first = config.settings()
            os.environ["MODEL_TEMPERATURE"] = "0.9"
            self.assertIs(config.settings(), first)
        self.assertEqual(first.temperature, 0.1)

    def test_the_runtime_template_supplies_every_setting_start_up_requires(self):
        """Check the deployed Runtime's environment passes start-up validation: the template sets every value."""
        template = load("checked_cloudformation", "deployment/validate_infra_baseline.py").load_template(
            (ROOT / "backend/deployment/templates/nested/4-agentcore-resources.yaml").read_text())
        variables = template["Resources"]["AgentCoreRuntime"]["Properties"]["EnvironmentVariables"]
        # The template's literal values. Each !Ref or !GetAtt resolves at deploy time, and "1" stands in for it (a
        # published guardrail version number too). The image sets AGENTCORE_RUNTIME (backend/runtime/Dockerfile).
        env = {name: value if isinstance(value, str) else "1" for name, value in variables.items()}
        with patch.dict(os.environ, {**env, "AGENTCORE_RUNTIME": "true"}, clear=True):
            config = load("checked_model_config", "backend/runtime/utils/model_config.py")
            model = config.settings()
        self.assertEqual((set(model.model_ids), model.temperature), (set(config.MODEL_ID_SETTINGS), 0.3))
        # The entrypoint's own start-up variables, and the Region, are set too.
        entrypoint = ast.parse((ROOT / "backend/runtime/wfm_agui_entrypoint.py").read_text())
        required = next(ast.literal_eval(node.value) for node in entrypoint.body if isinstance(node, ast.Assign)
                        and getattr(node.targets[0], "id", None) == "REQUIRED_ENV_VARS")
        self.assertLessEqual({*required, "DEPLOYED_DeploymentRegion"}, set(variables))

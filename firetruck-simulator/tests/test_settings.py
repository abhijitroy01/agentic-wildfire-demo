"""Firetruck settings: the container bind, the .env template, the ECS wiring and the operational limits."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("ENVIRONMENT", "local")
# Tests set operational limits through the environment; images have no deployment/settings.toml.
os.environ["FIRETRUCK_EVIDENCE_EVENTS_MAX"] = "50"
os.environ["FIRETRUCK_RUN_LIFETIME_MINUTES"] = "120"
os.environ["FIRETRUCK_RUNS_PER_TASK"] = "50"

from config import settings as settings_module
from pydantic import ValidationError

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"
# The local settings template; images and the release bundle drop dot-files, so it exists only in a checkout.
ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
# Each operational limit with the value, minimum and maximum this test's own settings file gives it.
LIMITS = {"firetruck_evidence_events_max": (500, 50, 5000), "firetruck_run_lifetime_minutes": (90, 10, 120),
          "firetruck_runs_per_task": (40, 1, 200)}
SPEC = "".join(f'[capacity.{name}]\nvalue = {value}\nunit = "u"\nmin = {low}\nmax = {high}\ndescription = "d"\n'
               for name, (value, low, high) in LIMITS.items())


def settings(**values):
    """Build settings from the environment and explicit values only, ignoring any local .env file."""
    return settings_module.Settings(_env_file=None, **values)


class EnvTemplateTests(unittest.TestCase):
    @unittest.skipUnless(ENV_EXAMPLE.exists(), "this copy has no .env.example")
    def test_a_env_copied_from_the_template_loads(self):
        """Settings rejects keys it does not declare, so the template lists only its fields, set or commented."""
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", text, re.MULTILINE))
        accepted = {(field.validation_alias or name).upper()
                    for name, field in settings_module.Settings.model_fields.items()}
        self.assertIn("ENVIRONMENT", listed)
        self.assertEqual(sorted(listed - accepted), [])
        with tempfile.TemporaryDirectory() as folder:
            copied = Path(folder) / ".env"
            copied.write_text(text, encoding="utf-8")
            self.assertEqual(settings_module.Settings(_env_file=copied).environment, "local")

    def test_a_env_from_an_older_template_is_refused_naming_each_removed_key(self):
        """LOG_LEVEL and CORS_ORIGINS left with the local REST API (FTRK-1), so a .env that still sets one fails."""
        for line in ("LOG_LEVEL=DEBUG", "CORS_ORIGINS=http://localhost:3000"):
            key = line.partition("=")[0]
            with self.subTest(key=key), tempfile.TemporaryDirectory() as folder:
                copied = Path(folder) / ".env"
                copied.write_text(f"ENVIRONMENT=local\n{line}\n", encoding="utf-8")
                with self.assertRaises(ValidationError) as refused:
                    settings_module.Settings(_env_file=copied)
                self.assertEqual([(error["type"], error["loc"]) for error in refused.exception.errors()],
                                 [("extra_forbidden", (key.lower(),))])


class BindHostTests(unittest.TestCase):
    @unittest.skipUnless(DOCKERFILE.exists(), "this copy has no Dockerfile")
    def test_container_command_binds_every_interface_explicitly(self):
        command = json.loads(re.search(r"^CMD (\[.*\])$", DOCKERFILE.read_text(), re.MULTILINE).group(1))
        bind = command[command.index("--host") + 1]
        self.assertEqual(bind, "0.0.0.0")  # nosec B104 asserts the container's address; nothing binds here


class ServiceWiringTests(unittest.TestCase):
    def test_ecs_requires_table_bucket_region_and_service_key(self):
        # The key is built at run time; the stack's real key comes from Secrets Manager.
        key = "k" * 40
        wiring = {"AWS_REGION": "us-west-2", "FIRETRUCK_TABLE": "runs", "FIRETRUCK_BUCKET": "evidence",
                  "FIRETRUCK_SERVICE_KEY": key}
        with mock.patch.dict(os.environ, wiring):
            loaded = settings(environment="ecs")
        self.assertEqual((loaded.aws_region, loaded.evidence_table, loaded.evidence_bucket),
                         ("us-west-2", "runs", "evidence"))
        self.assertEqual(loaded.service_key.get_secret_value(), key)
        self.assertNotIn(key, repr(loaded))
        for name in wiring:
            with self.subTest(missing=name), mock.patch.dict(os.environ, wiring):
                os.environ.pop(name)
                with self.assertRaisesRegex(RuntimeError, f"{name} is required"):
                    settings(environment="ecs")
        with mock.patch.dict(os.environ, {**wiring, "FIRETRUCK_SERVICE_KEY": "k" * 31}), \
                self.assertRaisesRegex(RuntimeError, "FIRETRUCK_SERVICE_KEY must have at least 32 characters"):
            settings(environment="ecs")
        # Local runs and tests inject their store, so nothing here is required outside ECS.
        with mock.patch.dict(os.environ):
            for name in wiring:
                os.environ.pop(name, None)
            self.assertIsNone(settings().evidence_table)


class OperationalLimitTests(unittest.TestCase):
    def test_every_firetruck_field_is_a_checked_operational_limit(self):
        fields = {name for name in settings_module.Settings.model_fields if name.startswith("firetruck_")}
        self.assertEqual(fields, set(settings_module.OPERATIONAL_LIMITS))
        self.assertEqual(fields, set(LIMITS))

    def test_ecs_requires_each_variable(self):
        for name in settings_module.OPERATIONAL_LIMITS:
            with self.subTest(name=name), mock.patch.dict(os.environ):
                os.environ.pop(name.upper(), None)
                with self.assertRaisesRegex(RuntimeError, f"{name.upper()} is required"):
                    settings(environment="ecs")

    def test_malformed_or_out_of_range_values_name_the_setting(self):
        self.assertEqual(settings(firetruck_evidence_events_max="75").firetruck_evidence_events_max, 75)
        for name in settings_module.OPERATIONAL_LIMITS:
            for value in ("abc", "1.5", "-5", "0", True):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(
                        RuntimeError, f"{name.upper()} must be a whole number"):
                    settings(**{name: value})

    def test_local_runs_read_the_values_and_ranges_from_deployment_settings(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ), \
                mock.patch.object(settings_module, "SETTINGS_FILE", Path(folder) / "settings.toml"):
            for name in LIMITS:
                os.environ.pop(name.upper(), None)
            with self.assertRaisesRegex(RuntimeError, "FIRETRUCK_EVIDENCE_EVENTS_MAX is required"):
                settings()
            settings_module.SETTINGS_FILE.write_text(SPEC, encoding="utf-8")
            loaded = settings()
            self.assertEqual({name: getattr(loaded, name) for name in LIMITS},
                             {name: value for name, (value, _, _) in LIMITS.items()})
            for name, (_, low, high) in LIMITS.items():
                with self.subTest(name=name), self.assertRaisesRegex(
                        RuntimeError, f"{name.upper()} must be a whole number from {low} to {high}"):
                    settings(**{name: high + 1})


if __name__ == "__main__":
    unittest.main()

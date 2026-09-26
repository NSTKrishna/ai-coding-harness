import tempfile
import unittest
from pathlib import Path

from harness import config as cfg
from harness.config import ConfigError, load_config, parse_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FAKE_KEY = "test-key-7f3a9c1e5b"
NO_DOTENV = Path("/nonexistent/harness/.env")


class LoadConfigTest(unittest.TestCase):
    def test_defaults_when_only_api_key_is_set(self):
        config = load_config({"AI_API_KEY": FAKE_KEY}, NO_DOTENV)

        self.assertEqual(config.api_key, FAKE_KEY)
        self.assertEqual(config.model.provider, cfg.DEFAULT_MODEL_PROVIDER)
        self.assertEqual(config.model.name, cfg.DEFAULT_MODEL)
        self.assertEqual(config.model.base_url, cfg.DEFAULT_BASE_URL)
        self.assertEqual(config.limits.max_steps, cfg.DEFAULT_MAX_STEPS)
        self.assertEqual(config.limits.max_repair_cycles, cfg.DEFAULT_MAX_REPAIR_CYCLES)
        self.assertEqual(config.limits.command_timeout_seconds, cfg.DEFAULT_COMMAND_TIMEOUT_SECONDS)
        self.assertIsNone(config.env_file)

    def test_missing_api_key_gives_actionable_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config({}, NO_DOTENV)
        message = str(ctx.exception)
        self.assertIn("AI_API_KEY is not set", message)
        self.assertIn("export AI_API_KEY", message)
        self.assertIn(".env.example", message)

    def test_blank_api_key_counts_as_missing(self):
        with self.assertRaises(ConfigError):
            load_config({"AI_API_KEY": "   "}, NO_DOTENV)

    def test_model_settings_come_from_environment(self):
        config = load_config(
            {
                "AI_API_KEY": FAKE_KEY,
                "AI_MODEL_PROVIDER": "some-provider",
                "AI_MODEL": "some-model",
                "AI_BASE_URL": "https://models.example.test/v1",
            },
            NO_DOTENV,
        )
        self.assertEqual(config.model.provider, "some-provider")
        self.assertEqual(config.model.name, "some-model")
        self.assertEqual(config.model.base_url, "https://models.example.test/v1")

    def test_invalid_base_url_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config({"AI_API_KEY": FAKE_KEY, "AI_BASE_URL": "models.example.test"}, NO_DOTENV)
        self.assertIn("AI_BASE_URL", str(ctx.exception))

    def test_limits_come_from_environment(self):
        config = load_config(
            {
                "AI_API_KEY": FAKE_KEY,
                "HARNESS_MAX_STEPS": "12",
                "HARNESS_MAX_REPAIR_CYCLES": "1",
                "HARNESS_COMMAND_TIMEOUT_SECONDS": "30",
            },
            NO_DOTENV,
        )
        self.assertEqual(config.limits.max_steps, 12)
        self.assertEqual(config.limits.max_repair_cycles, 1)
        self.assertEqual(config.limits.command_timeout_seconds, 30)

    def test_invalid_limits_name_the_variable(self):
        for value in ("abc", "0", "-3", "1.5"):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as ctx:
                    load_config({"AI_API_KEY": FAKE_KEY, "HARNESS_MAX_STEPS": value}, NO_DOTENV)
                self.assertIn("HARNESS_MAX_STEPS", str(ctx.exception))

    def test_repr_never_contains_api_key(self):
        config = load_config({"AI_API_KEY": FAKE_KEY}, NO_DOTENV)
        self.assertNotIn(FAKE_KEY, repr(config))
        self.assertNotIn(FAKE_KEY, str(config))

    def test_redact_replaces_api_key(self):
        config = load_config({"AI_API_KEY": FAKE_KEY}, NO_DOTENV)
        self.assertEqual(config.redact(f"key={FAKE_KEY}!"), "key=***!")


class DotenvTest(unittest.TestCase):
    def write_dotenv(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_dotenv_supplies_missing_values(self):
        path = self.write_dotenv(f"AI_API_KEY={FAKE_KEY}\nAI_MODEL=from-dotenv\n")
        config = load_config({}, path)
        self.assertEqual(config.api_key, FAKE_KEY)
        self.assertEqual(config.model.name, "from-dotenv")
        self.assertEqual(config.env_file, path)

    def test_environment_wins_over_dotenv(self):
        path = self.write_dotenv("AI_API_KEY=dotenv-key\nAI_MODEL=from-dotenv\n")
        config = load_config({"AI_API_KEY": FAKE_KEY, "AI_MODEL": "from-env"}, path)
        self.assertEqual(config.api_key, FAKE_KEY)
        self.assertEqual(config.model.name, "from-env")

    def test_parse_handles_comments_export_and_quotes(self):
        parsed = parse_dotenv(
            "# comment\n"
            "\n"
            "export A=1\n"
            "B=\"two words\"\n"
            "C='single'\n"
            "D=value # trailing comment\n"
            "E=\n"
        )
        self.assertEqual(parsed, {"A": "1", "B": "two words", "C": "single", "D": "value", "E": ""})

    def test_malformed_line_error_does_not_echo_content(self):
        secret_looking = "this-line-has-no-equals-sign-" + FAKE_KEY
        with self.assertRaises(ConfigError) as ctx:
            parse_dotenv(f"A=1\n{secret_looking}\n")
        self.assertIn("line 2", str(ctx.exception))
        self.assertNotIn(FAKE_KEY, str(ctx.exception))

    def test_env_example_has_no_credential(self):
        parsed = parse_dotenv((PROJECT_ROOT / ".env.example").read_text(encoding="utf-8"))
        self.assertIn("AI_API_KEY", parsed)
        self.assertEqual(parsed["AI_API_KEY"], "")
        for name in ("AI_MODEL_PROVIDER", "AI_MODEL", "AI_BASE_URL"):
            self.assertEqual(parsed[name], "", name)


if __name__ == "__main__":
    unittest.main()

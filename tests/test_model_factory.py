import unittest
from unittest import mock

from harness.config import ModelSettings
from harness.model import ScriptedModel
from harness.model import factory
from harness.model.factory import UnsupportedProviderError, create_model_client


class ModelFactoryTest(unittest.TestCase):
    def test_no_adapter_is_shipped(self):
        self.assertEqual(factory.ADAPTERS, {})

    def test_unset_provider(self):
        with self.assertRaises(UnsupportedProviderError) as ctx:
            create_model_client(ModelSettings(None, None, None), "key")
        self.assertIn("No model provider is configured", str(ctx.exception))

    def test_unsupported_provider(self):
        with self.assertRaises(UnsupportedProviderError) as ctx:
            create_model_client(ModelSettings("acme", "m1", None), "secret-value")
        message = str(ctx.exception)
        self.assertIn("Configured model provider is not supported by this build: 'acme'", message)
        self.assertNotIn("secret-value", message)

    def test_registered_adapter_is_used(self):
        built = []

        def adapter(settings, api_key):
            built.append((settings.provider, api_key))
            return ScriptedModel()

        with mock.patch.dict(factory.ADAPTERS, {"acme": adapter}):
            client = create_model_client(ModelSettings("acme", "m1", None), "k")
        self.assertIsInstance(client, ScriptedModel)
        self.assertEqual(built, [("acme", "k")])


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest import mock

from harness.config import ModelSettings
from harness.model import ScriptedModel
from harness.model import factory
from harness.model.adapters.openai_compatible import OpenAICompatibleClient
from harness.model.factory import UnsupportedProviderError, create_model_client


def settings(adapter=None, provider=None, model=None, base_url=None, **kw):
    return ModelSettings(provider, model, base_url, adapter=adapter, **kw)


class ModelFactoryTest(unittest.TestCase):
    def test_adapters_are_keyed_by_transport_not_model_family(self):
        self.assertEqual(set(factory.ADAPTERS), {"openai_compatible"})

    def test_unset_adapter(self):
        with self.assertRaises(UnsupportedProviderError) as ctx:
            create_model_client(settings(), "key")
        self.assertIn("No model provider is configured", str(ctx.exception))
        self.assertIn("AI_MODEL_ADAPTER", str(ctx.exception))

    def test_unsupported_adapter(self):
        with self.assertRaises(UnsupportedProviderError) as ctx:
            create_model_client(settings("acme", model="m1"), "secret-value")
        message = str(ctx.exception)
        self.assertIn("Configured model adapter is not supported by this build: 'acme'", message)
        self.assertIn("openai_compatible", message)
        self.assertNotIn("secret-value", message)

    def test_openai_compatible_requires_model_and_base_url(self):
        for kw, missing in (({}, "AI_MODEL and AI_BASE_URL"), ({"model": "m"}, "AI_BASE_URL"),
                            ({"base_url": "http://x"}, "AI_MODEL")):
            with self.subTest(kw=kw), self.assertRaises(UnsupportedProviderError) as ctx:
                create_model_client(settings("openai_compatible", **kw), "secret-value")
            self.assertIn(missing, str(ctx.exception))
            self.assertNotIn("secret-value", str(ctx.exception))

    def test_openai_compatible_is_built_from_settings(self):
        client = create_model_client(settings("openai_compatible", provider="any-family", model="model-x",
                                              base_url="http://gateway.test/v1", timeout_seconds=7, max_retries=1,
                                              headers=(("X-Team", "t1"),), structured_output="json_object"),
                                     "secret-value")
        self.assertIsInstance(client, OpenAICompatibleClient)
        self.assertEqual((client.model, client.base_url, client.timeout_seconds, client.max_retries),
                         ("model-x", "http://gateway.test/v1", 7, 1))
        self.assertEqual(client.provider, "any-family")
        self.assertEqual(client.extra_headers, (("X-Team", "t1"),))
        self.assertNotIn("secret-value", repr(client))

    def test_model_family_never_selects_the_adapter(self):
        # provider/model names that look like known families must not change dispatch
        for provider, model in (("deepseek", "deepseek-anything"), ("qwen", "qwen-anything")):
            with self.subTest(provider=provider), self.assertRaises(UnsupportedProviderError):
                create_model_client(settings(None, provider=provider, model=model, base_url="http://x"), "k")

    def test_registered_adapter_is_used(self):
        built = []

        def adapter(s, api_key):
            built.append((s.adapter, api_key))
            return ScriptedModel()

        with mock.patch.dict(factory.ADAPTERS, {"acme": adapter}):
            client = create_model_client(settings("acme", model="m1"), "k")
        self.assertIsInstance(client, ScriptedModel)
        self.assertEqual(built, [("acme", "k")])


if __name__ == "__main__":
    unittest.main()

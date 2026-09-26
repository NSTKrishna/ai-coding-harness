import unittest

from harness.repo.signals import extract_task_signals, keyword_stem, normalize_name, split_identifier


class TaskSignalsTest(unittest.TestCase):
    def test_example_from_the_brief(self):
        s = extract_task_signals("Fix PaymentService refreshToken so expired tokens are retried")
        self.assertEqual(s.identifiers, ("PaymentService", "refreshToken"))
        self.assertEqual(s.keywords, ("expired", "tokens", "retried"))
        self.assertEqual(s.phrases, ("expired tokens",))
        self.assertEqual(s.name_parts, ("payment", "service", "refresh", "token"))
        self.assertEqual(s.explicit_paths, ())

    def test_explicit_paths(self):
        s = extract_task_signals("Crash in src/auth/token.py:42 (see ./scripts/run.sh and setup.cfg). "
                                 "Docs: https://example.com/a/b.py")
        self.assertEqual(s.explicit_paths, ("src/auth/token.py", "scripts/run.sh", "setup.cfg"))

    def test_identifier_styles(self):
        s = extract_task_signals("`parse_value` fails in AuthenticationManager.getUserById() when MAX_RETRIES is 0; "
                                 "also check load_config() and HTTPClient")
        for ident in ("parse_value", "AuthenticationManager.getUserById", "AuthenticationManager", "getUserById",
                      "MAX_RETRIES", "load_config", "HTTPClient"):
            self.assertIn(ident, s.identifiers)

    def test_ordinary_keywords_drop_noise(self):
        s = extract_task_signals("Please fix the bug where the invoice total is wrong and should be rounded")
        self.assertEqual(s.keywords, ("invoice", "total", "rounded"))
        self.assertEqual(s.identifiers, ())

    def test_phrases_do_not_cross_punctuation(self):
        s = extract_task_signals("ParseConfig panics on empty input; see internal/parser/parser.go")
        self.assertEqual(s.phrases, ("empty input",))

    def test_deterministic(self):
        task = "Fix PaymentService.refresh_token so `expired` tokens in src/pay/service.py are retried"
        self.assertEqual(extract_task_signals(task), extract_task_signals(task))

    def test_empty_task(self):
        self.assertTrue(extract_task_signals("please fix it").empty)

    def test_helpers(self):
        self.assertEqual(split_identifier("HTTPClientError"), ["http", "client", "error"])
        self.assertEqual(split_identifier("refresh_token"), ["refresh", "token"])
        self.assertEqual(normalize_name("Parse-Config"), normalize_name("parse_config"))
        self.assertEqual([keyword_stem(w) for w in ("tokens", "retried", "parsing", "status", "class")],
                         ["token", "retr", "pars", "status", "class"])


if __name__ == "__main__":
    unittest.main()

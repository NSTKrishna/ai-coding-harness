import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness.config import ContextLimits
from harness.repo import discover_for_task
from harness.repo.discovery import base_key, identifier_variants

from tests.repo_fixtures import GO_REPO, NON_GIT_REPO, PYTHON_REPO, TS_REPO, git_repo, large_repo, snapshot, write_files


def ranked(result):
    return [c.path for c in result.candidates]


class DiscoveryCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()


@unittest.skipUnless(shutil.which("git"), "git not installed")
class FixtureRankingTest(DiscoveryCase):
    def test_python_service_and_its_test_rank_first(self):
        result = discover_for_task(git_repo(self.base / "py", PYTHON_REPO), "Fix PaymentService refresh_token behavior")
        self.assertEqual(ranked(result)[:2], ["src/payment/service.py", "tests/test_service.py"])
        top = result.candidates[0]
        self.assertIn("defines PaymentService (line 4)", top.reasons)
        self.assertIn("defines refresh_token (line 8)", top.reasons)
        self.assertIn("test for top candidate src/payment/service.py", result.candidates[1].reasons)
        self.assertNotIn("src/utils/strings.py", ranked(result))

    def test_typescript_token_and_test_rank_first(self):
        result = discover_for_task(git_repo(self.base / "ts", TS_REPO),
                                   "token refresh fails when the access token has expired")
        self.assertEqual(set(ranked(result)[:2]), {"src/auth/token.ts", "src/auth/token.test.ts"})
        self.assertEqual(result.metrics.keyword_search, "run")
        for path in ("src/ui/button.tsx", "src/ui/theme.ts"):
            self.assertNotIn(path, ranked(result))

    def test_go_parser_files_rank_first(self):
        result = discover_for_task(git_repo(self.base / "go", GO_REPO), "ParseConfig returns an error for empty files")
        self.assertEqual(ranked(result)[:2], ["internal/parser/parser.go", "internal/parser/parser_test.go"])
        self.assertIn("defines ParseConfig (line 5)", result.candidates[0].reasons)
        self.assertNotIn("internal/server/server.go", ranked(result))

    def test_every_candidate_has_reasons(self):
        for fixture, task in ((PYTHON_REPO, "Fix PaymentService refresh_token behavior"),
                              (TS_REPO, "token refresh fails"), (GO_REPO, "ParseConfig breaks")):
            result = discover_for_task(git_repo(self.base / str(len(fixture)), fixture), task)
            for c in result.candidates:
                with self.subTest(path=c.path):
                    self.assertTrue(c.reasons)
                    self.assertTrue(all(isinstance(r, str) and r for r in c.reasons))


@unittest.skipUnless(shutil.which("git"), "git not installed")
class RankingRulesTest(DiscoveryCase):
    def setUp(self):
        super().setUp()
        self.root = git_repo(self.base / "py", PYTHON_REPO)

    def test_explicit_path_ranks_first(self):
        result = discover_for_task(self.root, "Update src/utils/strings.py so PaymentService slugs are stable")
        self.assertEqual(ranked(result)[0], "src/utils/strings.py")
        self.assertIn("explicit path in task: src/utils/strings.py", result.candidates[0].reasons)

    def test_identifier_match_outranks_keyword_match(self):
        write_files(self.root, {"src/payment/notes.py": "# the ledger records totals\nLEDGER_NOTE = 'ledger'\n"})
        result = discover_for_task(self.root, "Ledger totals are recorded twice")
        order = ranked(result)
        self.assertLess(order.index("src/payment/ledger.py"), order.index("src/payment/notes.py"))

    def test_unknown_path_produces_warning(self):
        result = discover_for_task(self.root, "Fix src/missing/thing.py")
        self.assertIn("task mentions 'src/missing/thing.py', which is not in the repository inventory",
                      result.warnings)

    def test_import_expansion_is_one_hop(self):
        result = discover_for_task(self.root, "Fix PaymentService refresh_token behavior")
        tokens = next(c for c in result.candidates if c.path == "src/payment/tokens.py")
        self.assertTrue(any(r.startswith("imported by top candidate src/payment/service.py") for r in tokens.reasons))

    def test_documentation_weighted_down(self):
        result = discover_for_task(self.root, "Fix PaymentService refresh_token behavior")
        readme = next(c for c in result.candidates if c.path == "README.md")
        self.assertIn("documentation file (score halved)", readme.reasons)
        self.assertLess(readme.score, result.candidates[1].score)

    def test_deterministic(self):
        task = "Fix PaymentService refresh_token behavior"
        first, second = discover_for_task(self.root, task), discover_for_task(self.root, task)
        self.assertEqual(first.candidates, second.candidates)
        self.assertEqual(first.working_set.render(), second.working_set.render())

    def test_no_signals(self):
        result = discover_for_task(self.root, "please fix it")
        self.assertEqual(result.candidates, ())
        self.assertIn("no usable signals in the task (no paths, identifiers or keywords)", result.warnings)


class PairingKeyTest(unittest.TestCase):
    def test_base_key(self):
        pairs = [("src/foo.py", "tests/test_foo.py"), ("pkg/foo.py", "pkg/foo_test.py"),
                 ("src/foo.ts", "src/foo.test.ts"), ("src/foo.ts", "src/foo.spec.ts"),
                 ("pkg/foo.go", "pkg/foo_test.go"), ("src/main/java/Foo.java", "src/test/java/FooTest.java")]
        for source, test in pairs:
            with self.subTest(source=source):
                self.assertEqual(base_key(source), base_key(test))

    def test_identifier_variants(self):
        self.assertEqual(identifier_variants("refresh_token"),
                         ["refresh_token", "refreshToken", "RefreshToken"])
        self.assertEqual(identifier_variants("MAX_RETRIES"), ["MAX_RETRIES"])


class PairingBoostTest(DiscoveryCase):
    def test_source_and_test_boost_each_other(self):
        write_files(self.base / "r", {
            "lib/alpha.go": "package lib\n\nfunc ComputeAlpha() {}\n",
            "lib/alpha_test.go": "package lib\n\nimport \"testing\"\n\nfunc TestSomething(t *testing.T) {}\n",
            "lib/beta.go": "package lib\n\nfunc Beta() {}\n",
        })
        # The test file never mentions ComputeAlpha: only the naming convention links it.
        result = discover_for_task(self.base / "r", "ComputeAlpha is wrong")
        test = next(c for c in result.candidates if c.path == "lib/alpha_test.go")
        self.assertIn("test for top candidate lib/alpha.go", test.reasons)
        self.assertNotIn("lib/beta.go", ranked(result))


@unittest.skipUnless(shutil.which("git"), "git not installed")
class SelectivityAndSafetyTest(DiscoveryCase):
    def test_discovery_reads_few_files(self):
        root = large_repo(self.base / "big", filler=300)
        result = discover_for_task(root, "InvoiceParser.parse_total mishandles commas")
        m = result.metrics
        self.assertEqual(ranked(result)[:2], ["src/billing/invoice_parser.py", "tests/test_invoice_parser.py"])
        self.assertEqual(m.inventory_files, 303)
        self.assertLessEqual(m.discovery_files_read, 8)
        self.assertLess(m.analysis_files_read + m.discovery_files_read, 0.05 * m.inventory_files)
        self.assertLessEqual(m.files_matched, 5)
        self.assertEqual(m.candidates, len(result.candidates))
        self.assertTrue(m.keyword_search.startswith("skipped"))

    def test_common_keywords_are_ignored(self):
        root = large_repo(self.base / "big", filler=60)
        result = discover_for_task(root, "handler request is slow")
        self.assertTrue(any("ignored as too common" in w for w in result.warnings))
        self.assertLess(len(result.candidates), 10)

    def test_repository_is_not_modified(self):
        root = git_repo(self.base / "py", PYTHON_REPO)
        before = snapshot(root)
        discover_for_task(root, "Fix PaymentService refresh_token behavior")
        self.assertEqual(snapshot(root), before)

    def test_no_api_key_needed(self):
        root = git_repo(self.base / "go", GO_REPO)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_API_KEY", None)
            self.assertTrue(discover_for_task(root, "ParseConfig").candidates)

    def test_gitignored_files_are_never_candidates(self):
        root = git_repo(self.base / "py", {**PYTHON_REPO, ".gitignore": "scratch/\n"},
                        {"scratch/payment_copy.py": "class PaymentService: pass\n"})
        self.assertNotIn("scratch/payment_copy.py",
                         ranked(discover_for_task(root, "Fix PaymentService refresh_token behavior")))


class NonGitDiscoveryTest(DiscoveryCase):
    def test_filesystem_fallback_discovery(self):
        write_files(self.base / "plain", NON_GIT_REPO)
        result = discover_for_task(self.base / "plain", "compute doubles the wrong way")
        self.assertEqual(ranked(result)[:2], ["plain/core.py", "tests/test_core.py"])
        self.assertNotIn("build/lib/plain/core.py", ranked(result))


if __name__ == "__main__":
    unittest.main()

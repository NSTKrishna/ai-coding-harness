"""Miniature repositories for repository-intelligence tests.

Each builder writes files under ``base`` and, for git fixtures, commits the
tracked set (fixture git commands use the isolated config in tests.helpers).
"""

import hashlib
import json
import os
import textwrap
from pathlib import Path

from tests.helpers import git


def write_files(root: Path, files: dict) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")


def git_repo(root: Path, tracked: dict, untracked: dict = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_files(root, tracked)
    git(root, "init", "-q")
    git(root, "symbolic-ref", "HEAD", "refs/heads/main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture")
    write_files(root, untracked or {})
    return root


def snapshot(root: Path) -> dict:
    """Every file (including .git) -> (size, mtime_ns, sha256), to prove nothing changed."""
    state = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            data = path.read_bytes()
            stat = path.stat()
            state[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns, hashlib.sha256(data).hexdigest())
    return state


PYTHON_REPO = {
    "pyproject.toml": """
        [project]
        name = "payments"
        version = "0.1.0"

        [tool.pytest.ini_options]
        testpaths = ["tests"]

        [tool.ruff]
        line-length = 100
    """,
    "src/payment/__init__.py": "",
    "src/payment/service.py": """
        from payment.tokens import TokenStore


        class PaymentService:
            def __init__(self, store: TokenStore):
                self.store = store

            def refresh_token(self, account):
                token = self.store.get(account)
                if token.expired:
                    return None
                return token
    """,
    "src/payment/tokens.py": """
        class TokenStore:
            def __init__(self):
                self._tokens = {}

            def get(self, account):
                return self._tokens.get(account)
    """,
    "src/payment/ledger.py": """
        class Ledger:
            def record(self, amount):
                return amount
    """,
    "src/utils/strings.py": """
        def slugify(text):
            return text.lower().replace(" ", "-")
    """,
    "tests/test_service.py": """
        from payment.service import PaymentService


        def test_refresh_token_returns_none_for_expired():
            assert PaymentService is not None
    """,
    "tests/test_ledger.py": """
        from payment.ledger import Ledger


        def test_record():
            assert Ledger().record(3) == 3
    """,
    "README.md": "# Payments\n\nThe PaymentService handles payments.\n",
    ".github/workflows/ci.yml": "name: ci\non: [push]\n",
}

TS_REPO = {
    "package.json": json.dumps({
        "name": "auth",
        "scripts": {"test": "vitest run", "build": "tsc -p .", "lint": "eslint ."},
        "devDependencies": {"typescript": "^5.4.0", "vitest": "^1.6.0", "eslint": "^9.0.0"},
    }, indent=2),
    "tsconfig.json": '{"compilerOptions": {"strict": true}}\n',
    "src/auth/token.ts": """
        export interface Token { value: string; expiresAt: number }

        export function refreshToken(token: Token): Token {
          if (token.expiresAt < Date.now()) {
            throw new Error("expired");
          }
          return token;
        }
    """,
    "src/auth/token.test.ts": """
        import { refreshToken } from './token';

        describe('token refresh', () => {
          it('refreshes an expired token', () => {
            expect(refreshToken).toBeDefined();
          });
        });
    """,
    "src/auth/session.ts": """
        import { refreshToken, Token } from './token';

        export class Session {
          constructor(private token: Token) {}
          renew() { this.token = refreshToken(this.token); }
        }
    """,
    "src/ui/button.tsx": "export const Button = () => null;\n",
    "src/ui/theme.ts": "export const theme = { color: 'blue' };\n",
}

GO_REPO = {
    "go.mod": "module example.com/cfg\n\ngo 1.21\n",
    "internal/parser/parser.go": """
        package parser

        type Config struct{ Name string }

        func ParseConfig(data []byte) (*Config, error) {
        	return &Config{}, nil
        }
    """,
    "internal/parser/parser_test.go": """
        package parser

        import "testing"

        func TestParseConfig(t *testing.T) {
        	if _, err := ParseConfig(nil); err != nil {
        		t.Fatal(err)
        	}
        }
    """,
    "internal/server/server.go": "package server\n\nfunc Start() {}\n",
    "cmd/app/main.go": """
        package main

        import "example.com/cfg/internal/parser"

        func main() {
        	parser.ParseConfig(nil)
        }
    """,
}

NOISY_TRACKED = {
    ".gitignore": "generated/\n*.log\nbuild/\n",
    "src/app.py": "def main():\n    return 1\n",
}
NOISY_UNTRACKED = {
    "untracked_note.py": "NOTE = 1\n",
    "generated/out.py": "GENERATED = 1\n",
    "debug.log": "log line\n",
    "build/lib/app.py": "copy\n",
    "dist/bundle.js": "bundle\n",
    ".venv/lib/site.py": "venv\n",
    "node_modules/pkg/index.js": "module.exports = 1\n",
    "src/__pycache__/app.cpython-312.pyc": b"\x00\x01binary",
}

NON_GIT_REPO = {
    "setup.py": "from setuptools import setup\nsetup(name='plain')\n",
    "requirements.txt": "requests\n",
    "plain/__init__.py": "",
    "plain/core.py": "def compute(x):\n    return x * 2\n",
    "tests/test_core.py": "import unittest\nfrom plain.core import compute\n\n\n"
                          "class T(unittest.TestCase):\n    def test(self):\n        self.assertEqual(compute(2), 4)\n",
    "docs/guide.md": "# Guide\n",
    "build/lib/plain/core.py": "stale copy\n",
    ".venv/bin/activate": "venv\n",
    "node_modules/x/index.js": "x\n",
}


def large_repo(root: Path, filler: int = 300) -> Path:
    """A git repository with many unrelated files and one relevant module + test."""
    files = {
        "pyproject.toml": "[project]\nname = 'big'\n",
        "src/billing/invoice_parser.py": "class InvoiceParser:\n    def parse_total(self, text):\n        return 0\n",
        "tests/test_invoice_parser.py": "from billing.invoice_parser import InvoiceParser\n\n\n"
                                        "def test_total():\n    assert InvoiceParser().parse_total('') == 0\n",
    }
    for i in range(filler):
        files[f"src/module_{i:03d}/component_{i:03d}.py"] = (
            f"def handler_{i}(request):\n    return request.get('field_{i}')\n")
    return git_repo(root, files)

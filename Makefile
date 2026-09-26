# Evaluator flow:
#   export AI_API_KEY="..."
#   make setup
#   make run
#
# Override the interpreter with: make setup PYTHON=python3.12

PYTHON ?= python3
VENV := .venv
VENV_PY := $(VENV)/bin/python
STAMP := $(VENV)/.harness-setup
ARGS ?=

.PHONY: setup run test clean

setup:
	@$(PYTHON) -c 'import sys; sys.exit(0) if sys.version_info >= (3, 10) else sys.exit("error: Python >= 3.10 is required, found " + sys.version.split()[0] + ". Use: make setup PYTHON=python3.x")'
	@test -x $(VENV_PY) || $(PYTHON) -m venv $(VENV)
	@# Make src/ importable without network access.
	@$(VENV_PY) -c 'import pathlib, sysconfig; pathlib.Path(sysconfig.get_paths()["purelib"], "harness-src.pth").write_text(str(pathlib.Path("src").resolve()) + "\n")'
	@# Editable install only adds the `harness` console script; it may need network for setuptools.
	@$(VENV_PY) -m pip install --disable-pip-version-check --quiet --no-deps -e . \
	  || echo "warning: editable install failed (offline?). 'make run' and 'python -m harness' still work; the 'harness' command is unavailable."
	@$(VENV_PY) -c 'import harness, sys; print("harness", harness.__version__, "ready on Python", sys.version.split()[0])'
	@touch $(STAMP)

$(STAMP):
	@$(MAKE) --no-print-directory setup

run: $(STAMP)
	@$(VENV_PY) -m harness run $(ARGS)

test: $(STAMP)
	$(VENV_PY) -m unittest discover -s tests -t . -v

clean:
	rm -rf $(VENV) build dist src/*.egg-info .harness-runs .pytest_cache .mypy_cache .ruff_cache
	find . -path ./.git -prune -o -type d -name __pycache__ -prune -exec rm -rf {} +

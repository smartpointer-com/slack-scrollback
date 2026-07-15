# slack-scrollback — the sole build/test entry point.
#
# Every task a contributor or CI needs is a target here; there are no side
# scripts. The tool has no runtime dependencies, so the venv exists purely to
# keep the lint/test toolchain off the system Python.

PYTHON ?= python3
VENV   := .venv
PY     := $(VENV)/bin/python

# The interpreter baked into ./slack-scrollback's shebang. `env python3` keeps
# the artifact portable, but it resolves to whatever python3 comes first on the
# *running* user's PATH — on macOS that is /usr/bin/python3, still 3.9. Point
# this at a 3.11+ interpreter when building a copy for a user whose default
# python3 is too old:
#
#   make build PYTHON_SHEBANG=/opt/homebrew/bin/python3
PYTHON_SHEBANG ?= /usr/bin/env python3
DIST           := slack-scrollback

.DEFAULT_GOAL := all
.PHONY: all install lint test fmt build clean

all: install lint test build

## install — create .venv and install the package plus pinned dev tools.
install: $(VENV)/.stamp

# Keyed on pyproject.toml: the venv is rebuilt only when dependencies change.
$(VENV)/.stamp: pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet --editable ".[dev]"
	@touch $@

## lint — static checks: style, import order, formatting, types.
lint: install
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/ruff format --check src tests
	$(VENV)/bin/mypy

## test — unit tests. No network: the HTTP layer is stubbed throughout.
test: install
	$(VENV)/bin/pytest -q

## fmt — apply formatting and autofixable lint rules.
fmt: install
	$(VENV)/bin/ruff format src tests
	$(VENV)/bin/ruff check --fix src tests

## build — produce ./slack-scrollback: one self-contained executable file.
#
# Having no runtime dependencies is what makes this possible: the whole tool is
# a stdlib zipapp, so the artifact installs by being copied. No venv, no clone,
# no package manager, no container — just a file and a python3 to run it.
build: install
	@find src -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	$(PY) -m zipapp src --main "slack_scrollback.cli:main" --python "$(PYTHON_SHEBANG)" --output $(DIST)
	@chmod +x $(DIST)
	@echo "built ./$(DIST) ($$(du -h $(DIST) | cut -f1)) — copy this one file anywhere with python3.11+"

## clean — remove the venv, the artifact, and all tool caches.
clean:
	rm -rf $(VENV) $(DIST) .pytest_cache .mypy_cache .ruff_cache dist build
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.egg-info' -type d -prune -exec rm -rf {} +

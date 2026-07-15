# slack-scrollback — the sole build/test entry point.
#
# Every task a contributor or CI needs is a target here; there are no side
# scripts. The tool has no runtime dependencies, so the venv exists purely to
# keep the lint/test toolchain off the system Python.

PYTHON ?= python3
VENV   := .venv
PY     := $(VENV)/bin/python

.DEFAULT_GOAL := all
.PHONY: all install lint test fmt clean

all: install lint test

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

## clean — remove the venv and all tool caches.
clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache dist build
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.egg-info' -type d -prune -exec rm -rf {} +

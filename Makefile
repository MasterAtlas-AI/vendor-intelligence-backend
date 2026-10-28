# Convenience targets (Git Bash / macOS / Linux). Windows PowerShell users: see README for the raw commands.
PY ?= .venv/Scripts/python
ifeq ($(OS),)
PY = .venv/bin/python
endif

.PHONY: install migrate bootstrap dev worker test lint format

install:
	python -m venv .venv && $(PY) -m pip install -e ".[dev]"

migrate:
	$(PY) -m alembic upgrade head

bootstrap:
	$(PY) -m app.cli bootstrap

dev:
	$(PY) -m uvicorn app.main:app --reload --port 8000

worker:  # optional: only with JOB_RUNNER_ENABLED=false on the API
	$(PY) -m app.cli run-worker

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check app tests migrations && $(PY) -m ruff format --check app tests migrations && $(PY) -m mypy app

format:
	$(PY) -m ruff check --fix app tests migrations && $(PY) -m ruff format app tests migrations

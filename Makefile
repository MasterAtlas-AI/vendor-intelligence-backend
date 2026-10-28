# Convenience targets (Git Bash / macOS / Linux). Windows PowerShell users: see README for the raw commands.
PY ?= .venv/Scripts/python
ifeq ($(OS),)
PY = .venv/bin/python
endif

.PHONY: install migrate bootstrap dev worker beat test lint format

install:
	python -m venv .venv && $(PY) -m pip install -e ".[dev]"

migrate:
	$(PY) -m alembic upgrade head

bootstrap:
	$(PY) -m app.cli bootstrap

dev:
	$(PY) -m uvicorn app.main:app --reload --port 8000

worker:
	$(PY) -m celery -A app.workers.celery_app worker -Q default,ingest,ai_discovery,ai_enrich,reports -l info --pool=solo

beat:
	$(PY) -m celery -A app.workers.celery_app beat -l info

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check app tests migrations && $(PY) -m ruff format --check app tests migrations && $(PY) -m mypy app

format:
	$(PY) -m ruff check --fix app tests migrations && $(PY) -m ruff format app tests migrations

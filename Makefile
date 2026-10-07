VENV := .venv
PY := $(VENV)/bin/python

.DEFAULT_GOAL := help

.PHONY: help install run test cover lint format samples docker clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Create the virtualenv and install dev dependencies
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt

run:  ## Run the API with autoreload on http://127.0.0.1:8000
	$(PY) -m uvicorn app.main:app --reload --port 8000

test:  ## Run the test suite
	$(PY) -m pytest

cover:  ## Run the tests with a coverage report
	$(PY) -m pytest --cov=app --cov-report=term-missing

lint:  ## Check formatting and lint rules
	$(PY) -m ruff check app tests samples
	$(PY) -m ruff format --check app tests samples

format:  ## Apply formatting and safe lint fixes
	$(PY) -m ruff check --fix app tests samples
	$(PY) -m ruff format app tests samples

samples:  ## Write the sample Shapefile/KML/KMZ files into ./samples
	$(PY) -m samples.make_samples

docker:  ## Build and run the container on http://127.0.0.1:8000
	docker compose up --build

clean:  ## Remove caches, coverage output and uploaded files
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov var
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

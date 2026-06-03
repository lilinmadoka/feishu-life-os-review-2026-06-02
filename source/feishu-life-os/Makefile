.PHONY: install dev test run lint migrate demo

install:
	python -m pip install -e .

dev:
	python -m pip install -e .[dev]

test:
	pytest -q

lint:
	ruff check .

migrate:
	python -m app.seed --migrate-only

run:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

demo:
	python -m app.seed

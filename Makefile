.PHONY: install test lint format typecheck check build clean package packaging-check

install:
	python -m pip install -e ".[dev,mcp]"

test:
	pytest --cov=tgv --cov-report=term-missing

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

typecheck:
	mypy src/tgv

check: lint typecheck test

build:
	python -m build

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov build dist *.egg-info src/*.egg-info
	find . -name "__pycache__" -exec rm -rf {} +
	find . -name "*.tmp" -delete

packaging-check: clean
	python scripts/check_packaging_cleanliness.py
	python scripts/check_version.py

package: packaging-check
	python -m build

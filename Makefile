.PHONY: check fmt lint typecheck test

check: lint typecheck test ## Run every gate: format check, lint, types, tests

fmt: ## Auto-format and fix lint violations
	uv run ruff format .
	uv run ruff check --fix .

lint: ## Check formatting and lint rules
	uv run ruff format --check .
	uv run ruff check .

typecheck: ## Run mypy in strict mode
	uv run mypy

test: ## Run the test suite with coverage
	uv run pytest

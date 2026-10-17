.PHONY: install seed sandbox test

install:
	uv sync

seed:
	uv run python -m sandbox.seed

sandbox: seed
	uv run python -m sandbox.run

test:
	uv run pytest

.PHONY: install seed sandbox test ui workspace

install:
	uv sync

seed:
	uv run python -m sandbox.seed

sandbox: seed
	uv run python -m sandbox.run

test:
	uv run pytest

ui:
	cd apps/workspace && npm install --no-audit --no-fund && npm run build

workspace:
	@test -f apps/workspace/dist/index.html || $(MAKE) ui
	uv run python -m apps.api

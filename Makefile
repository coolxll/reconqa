.PHONY: install dev-install tools doctor lint test serve docker-build docker-up docker-java-runner clean

install:
	uv sync

dev-install:
	uv sync --extra dev

tools:
	chmod +x scripts/install-tools.sh
	./scripts/install-tools.sh ./bin

doctor:
	uv run reconqa doctor

lint:
	uv run ruff check backend tests

test:
	uv run pytest -v

serve:
	uv run reconqa serve --host 0.0.0.0 --port 8000 --reload

docker-build:
	docker compose build api java-runner

docker-up:
	docker compose up -d api

docker-java-runner:
	docker compose --profile runner run --rm java-runner

clean:
	rm -rf .pytest_cache .ruff_cache dist build *.egg-info

.PHONY: install dev-install tools doctor lint test serve dev-web build-web docker-build docker-up docker-java-runner clean

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

dev-web:
	cd frontend && npm install && npm run dev

build-web:
	cd frontend && npm install && npm run build

docker-build:
	docker compose build api frontend java-runner

docker-up:
	docker compose up -d api frontend

docker-java-runner:
	docker compose --profile runner run --rm java-runner

clean:
	rm -rf .pytest_cache .ruff_cache dist build *.egg-info

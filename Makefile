.PHONY: dev test lint build infra-up infra-down

dev:
	./scripts/dev_up.sh

test:
	python3 -m pytest

lint:
	python3 -m ruff check .

build:
	python3 -m build

infra-up:
	./scripts/infra_up.sh

infra-down:
	./scripts/infra_down.sh

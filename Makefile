.PHONY: dev test infra-up infra-down

dev:
	./scripts/dev_up.sh

test:
	python3 -m pytest

infra-up:
	./scripts/infra_up.sh

infra-down:
	./scripts/infra_down.sh

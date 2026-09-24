.PHONY: build-lambda test lint check fmt ingest-fixtures

build-lambda:
	./scripts/build-lambda.sh

test:
	python3 -m pytest -q

lint:
	python3 -m ruff check backend tests

fmt:
	terraform -chdir=infra fmt -recursive
	terraform -chdir=infra/bootstrap fmt -recursive

check: test lint
	terraform -chdir=infra fmt -check -recursive
	terraform -chdir=infra/bootstrap fmt -check -recursive
	terraform -chdir=infra validate
	terraform -chdir=infra/bootstrap validate

ingest-fixtures:
	python3 scripts/ingest_fixtures.py
